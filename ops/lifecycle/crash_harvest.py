"""Moved out of the parent module to keep it inside the file-size ceiling."""

from __future__ import annotations

import logging
from enum import StrEnum

from base import telemetry
from base.agents import (
    AgentNotFound,
    CrashRecoveryResult,
)
from base.cluster.machine import machine_name
from base.db import Database
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops.lifecycle.events import (
    publish_inbound_arrived as publish_inbound_arrived,
)
from ops.lifecycle.events import (
    publish_notice_posted as publish_notice_posted,
)
from ops.lifecycle.events import (
    publish_notice_resolved as publish_notice_resolved,
)
from ops.lifecycle.events import (
    publish_page_closed as publish_page_closed,
)
from ops.lifecycle.launch import (
    _insert_prompt_blocking as _insert_prompt_blocking,
)
from ops.lifecycle.launch import (
    launch_agent_op as launch_agent_op,
)
from ops.lifecycle.termination import (
    _force_terminate_transaction as _force_terminate_transaction,
)
from ops.lifecycle.termination import (
    _publish_force_terminate_inbound as _publish_force_terminate_inbound,
)
from ops.lifecycle.termination import (
    force_mark_terminated as force_mark_terminated,
)
from ops.rpc_schemas import (
    RecoverCrashMarkedResponse,
)

_log = logging.getLogger(__name__)


class CrashRecoveryRequestFailure(StrEnum):
    """Local request failure, never a home runner adjudication."""

    UNREACHABLE = "unreachable"
    ERROR = "error"


def _recover_crash_marked_blocking(
    db: Database, bus: EventBus, agent_id: int
) -> RecoverCrashMarkedResponse:
    """Adjudicate one `recover-crash-marked-v2` harvest (task #3618).

    The requester is the delivery watchdog, escalating a chat inbound still
    `pending` past the stall threshold whose owner is a crash-marked idling
    corpse (`last_turn_fatal_at IS NOT NULL`). The harvest mirrors the corpse
    reaper's terminal shape (`agent/ownership/hosted.py::reap_crash_corpses`):
    status='terminated', termination_source='reaper', lease dropped, and the
    crash marker KEPT so the row still matches the relaxed
    `SYSTEM_REAPED_CRASH_ROW` trigger afterwards. The row lock plus the
    re-checked guards make the op idempotent and fail-closed:

    - missing row -> AgentNotFound;
    - the recovery breaker tripped (consecutive permanent provider
      rejections; `RECOVERY_BREAKER_CLEAR` inverted) -> refused with
      `permanent_provider_reject` — no automatic recovery may start;
    - an active wake-suppression window -> refused with its reason;
    - unmarked / non-idling / non-hosted / foreign machine / live lease ->
      refused, naming the guard that failed;
    - already terminated -> `already_terminated` (an idempotent repeat).
    """
    from base.agents.recovery_breaker import (
        HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
        SUPPRESS_REASON_PERMANENT_REJECT,
    )

    with db.write_transaction() as conn:
        row = conn.execute(
            "SELECT status, runtime_kind, machine, last_turn_fatal_at, "
            "(permanent_reject_streak >= %s), "
            "wake_suppress_reason, "
            "(wake_suppressed_until IS NOT NULL AND wake_suppressed_until >= now()), "
            "(lease_expires_at IS NOT NULL AND lease_expires_at > now()) "
            "FROM agents_meta WHERE id = %s FOR UPDATE",
            (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, agent_id),
        ).fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        (
            status,
            runtime_kind,
            machine,
            last_fatal_at,
            breaker_halted,
            suppress_reason,
            suppress_active,
            lease_alive,
        ) = row
        if breaker_halted:
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason=SUPPRESS_REASON_PERMANENT_REJECT
            )
        if suppress_active:
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason=suppress_reason or "wake_suppressed"
            )
        if last_fatal_at is None:
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason="not_marked"
            )
        if status == "terminated":
            return RecoverCrashMarkedResponse(status=CrashRecoveryResult.ALREADY_TERMINATED)
        if status != "idling":
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason=f"not_settled:{status}"
            )
        if runtime_kind != "hosted":
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED,
                reason=f"not_settled:runtime_kind={runtime_kind}",
            )
        if machine != machine_name():
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason="wrong_machine"
            )
        if lease_alive:
            return RecoverCrashMarkedResponse(
                status=CrashRecoveryResult.REFUSED, reason="lease_alive"
            )
        conn.execute(
            "UPDATE agents_meta SET status = 'terminated', "
            "termination_source = 'reaper', lease_expires_at = NULL, "
            "runtime_protocol_version = 0 "
            "WHERE id = %s AND status = 'idling' AND last_turn_fatal_at IS NOT NULL",
            (agent_id,),
        )
        prepared_event = prepare_event_log(
            event_type="status_change",
            agent_id=agent_id,
            source="system",
            payload={"from": "idling", "to": "terminated", "reason": "corpse_reaper"},
        )
        from base.agents.impersonation_manifest import record_central_event

        prepared_event = record_audit(conn, record_central_event(conn, prepared_event))
    telemetry.emit_prepared(prepared_event)
    _log.info(
        "recover-crash-marked-v2: harvested crash-marked corpse for agent %s "
        "(termination_source=reaper; the relaxed trigger resumes its queued work)",
        agent_id,
    )
    try:
        publish_agent_updated_sync(bus, agent_id)
    except Exception:
        _log.exception(
            "recover-crash-marked-v2: lifecycle hint publish failed for agent %s", agent_id
        )
    return RecoverCrashMarkedResponse(status=CrashRecoveryResult.HARVESTED)
