"""Resume a terminated agent by preserving its identity and enqueuing a wake."""

from datetime import datetime
from typing import Literal

import psycopg
from psycopg import sql

from ops.resurrection_retry import ResurrectExitDeferredError
from ops.resurrection_retry import ResurrectTriggerStaleError as ResurrectTriggerStaleError
from ops.resurrection_retry import lock_active_home_machine as _lock_active_home_machine
from shared.agents import (
    AgentNotFound,
    AgentStatus,
    MachinePaused,
    ResurrectAlreadyAlive,
    ResurrectBudgetExhausted,
    ResurrectRefused,
)
from shared.audit_events import insert_event_log
from shared.config import field_alias, get_field, settings
from shared.db import fetch_one, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.lifecycle_termination_observe import observe_applied_termination
from shared.live_announce import publish_agent_updated_sync
from shared.log import logger
from shared.machine import machine_name


def _transition_terminated_to_unclaimed_idling(
    cur: psycopg.Cursor,
    agent_id: int,
    *,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None,
) -> datetime:
    """Run the one final resurrection CAS with a fully static SQL shape.

    The automatic (trigger) branch refuses a closed agent — the closure marker
    re-checked under the row lock, so a close landing while a wake was in
    flight still wins. The explicit branch clears the closure marker: reopening
    a closed agent is exactly the manual resurrect's contract, and the caller
    reports it on the resurrect event.
    """
    base_params = (AgentStatus.IDLING, agent_id, AgentStatus.TERMINATED)
    if trigger_inbound_id is not None:
        from shared.lifecycle_acceptance import (
            CLOSED_AGENT,
            FAILED_RESTART_FOR_CURRENT_TARGET,
            SYSTEM_REAPED_CRASH_ROW,
        )
        from shared.recovery_breaker import RECOVERY_BREAKER_CLEAR

        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL, "
                "last_turn_fatal_at = NULL, "
                "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL, "
                "runtime_protocol_version = 0 "
                "WHERE id = %s AND status = %s "
                "AND NOT {} "
                "AND (agents_meta.wake_suppressed_until IS NULL "
                "     OR agents_meta.wake_suppressed_until < now()) "
                "AND {} "
                "AND NOT {} "
                "AND EXISTS ("
                "  SELECT 1 FROM inbound_messages m "
                "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
                "    AND m.status = 'pending' AND m.kind = %s "
                "    AND (m.created_at > agents_meta.status_changed_at OR {}) "
                "    AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0)"
                ") RETURNING status_changed_at"
            ).format(
                sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET),
                sql.SQL(RECOVERY_BREAKER_CLEAR),
                sql.SQL(CLOSED_AGENT),
                sql.SQL(SYSTEM_REAPED_CRASH_ROW),
            ),
            (*base_params, trigger_inbound_id, trigger_inbound_kind),
        )
    else:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, closed_at = NULL, lease_expires_at = NULL, "
            "last_turn_fatal_at = NULL, "
            "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL, "
            "runtime_protocol_version = 0 "
            "WHERE id = %s AND status = %s RETURNING status_changed_at",
            base_params,
        )
    transition_row = cur.fetchone()
    if transition_row is not None:
        return transition_row[0]
    cur.execute(
        "SELECT home.paused_at IS NOT NULL "
        "FROM agents_meta a JOIN machines home ON home.name = a.machine "
        "WHERE a.id = %s",
        (agent_id,),
    )
    paused_row = cur.fetchone()
    if paused_row is not None and paused_row[0] is True:
        raise MachinePaused(
            f"agent {agent_id} home machine is paused; resume it before resurrecting"
        )
    if trigger_inbound_id is not None:
        raise ResurrectTriggerStaleError(
            f"agent {agent_id} trigger work no longer qualifies for its current "
            "termination; UPDATE affected 0 rows (stale work, suppressed automatic "
            "wakes, a tripped recovery breaker, or a closed agent)"
        )
    raise ResurrectAlreadyAlive(
        f"agent {agent_id} was concurrently modified after SELECT; UPDATE affected 0 rows"
    )


def _resurrect_event_target(resurrected_by: str) -> int | None:
    if not resurrected_by.startswith("agent:"):
        return None
    try:
        return int(resurrected_by.removeprefix("agent:"))
    except ValueError:
        return None


def _auto_resurrect_max_attempts() -> int:
    """Read the recovery budget after the gateway's runner-alias projection.

    Gateway processes omit runner-only aliases from their environment, while
    local hosted resurrection still runs this transaction in-process. The
    cluster `.env` remains the configuration authority in that profile.
    """
    from shared.runtime_config import read_env_aliases

    raw = read_env_aliases().get(field_alias("auto_resurrect_max_attempts"))
    if raw is not None:
        return int(raw)
    budget = get_field("auto_resurrect_max_attempts")
    if budget is not None:
        return int(budget)
    if settings.has_domain("daemon"):
        return settings.daemon.auto_resurrect_max_attempts
    raise RuntimeError("auto-resurrect budget has no configured daemon domain")


def _prepare_resurrect_attempt(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None,
    billing_recovery: bool = False,
) -> bool:
    """Commit resurrection and its optional prompt before waking the host.

    Returns whether this call reopened a closed agent (the explicit branch
    cleared `closed_at`), so the caller can record it on the resurrect event
    and the `agent_reopened` warning line.

    `billing_recovery=True` (the versioned `resurrect-billing-v1` action, task
    #3919) re-checks the billing-victim contract under the same row lock: a
    closed row or a row that is not a billing-class recovery-breaker halt is
    refused (`ResurrectRefused`) — the batch entry never crosses the closure
    marker and only reinstates the recorded billing cohort.
    """
    from shared.envelope import reject_unnegotiated_caller
    from shared.exec_owner_recovery import recover_local_resources
    from shared.lifecycle_acceptance import supersede_lifecycle_for_resurrect

    reject_unnegotiated_caller(resurrected_by)
    recover_local_resources(agent_id, machine_name())
    with write_transaction() as conn, conn.cursor() as cur:
        latched_machine = _lock_active_home_machine(cur, agent_id)
        cur.execute(
            "SELECT status,machine,closed_at,permanent_reject_streak,last_permanent_reject_reason "
            "FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if row[1] != latched_machine:
            raise ResurrectTriggerStaleError("resurrection placement changed after pause latch")
        current = AgentStatus(row[0])
        reopened = trigger_inbound_id is None and row[2] is not None
        if current is not AgentStatus.TERMINATED:
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} is in {current.value!r} state, not 'terminated'"
            )
        if billing_recovery:
            from shared.recovery_breaker import (
                HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
                PERMANENT_REJECT_REASON_BILLING,
            )

            if row[2] is not None:
                raise ResurrectRefused("closed")
            if (
                row[3] < HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS
                or row[4] != PERMANENT_REJECT_REASON_BILLING
            ):
                raise ResurrectRefused("not_billing_halted")
        if resurrected_by == "system":
            cur.execute(
                "SELECT count(*) FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'resurrect' AND status = 'pending'",
                (agent_id,),
            )
            pending_resurrects = int(fetch_one(cur, "resurrect: count pending lifecycle rows")[0])
            if pending_resurrects >= _auto_resurrect_max_attempts():
                raise ResurrectBudgetExhausted(
                    f"agent {agent_id} has exhausted its auto-resurrect budget"
                )
        # The resurrection inbound is inserted before the observation check so
        # its id can fence the new incarnation's epoch: every earlier unapplied
        # lifecycle command is settled as superseded right here (issue #2158),
        # and a command that never applied cannot defer this resurrection. A
        # refusal below rolls the whole transaction back - fence included.
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', %s) RETURNING id",
            (agent_id, resurrected_by),
        )
        resurrect_row = cur.fetchone()
        if resurrect_row is None:
            raise RuntimeError("resurrect lifecycle inbound INSERT returned no id")
        supersede_lifecycle_for_resurrect(conn, agent_id, resurrect_row[0])
        if not observe_applied_termination(conn, agent_id, machine_name()):
            raise ResurrectExitDeferredError(
                "outstanding lifecycle target has not been observed ended"
            )
        _transition_terminated_to_unclaimed_idling(
            cur,
            agent_id,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
        if prompt is not None:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'chat', %s)",
                (agent_id, prompt, resurrected_by),
            )
        conn.commit()
        publish_agent_updated_sync(agent_id)
    publish_inbound_wake(agent_id, "0")
    return reopened


def resurrect_agent(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None,
    billing_recovery: bool = False,
) -> int:
    """Atomically restore native intent and enqueue lifecycle plus optional chat.

    Pending-work callers name the exact post-termination inbound. Its ID and
    the latest force-termination fence are checked under the metadata row lock,
    and — for a row the system itself reaped after a crash
    (`SYSTEM_REAPED_CRASH_ROW`) — work that predates the termination still
    qualifies: a reaper death is not an operator's decision. The automatic
    trigger additionally requires clear automatic wakes (no suppression window,
    `RECOVERY_BREAKER_CLEAR`) and an open agent (no closure marker); explicit
    manual resurrection passes no trigger and keeps its unconditional
    contract — it reopens a closed agent (clearing `closed_at`), the audit
    event carries `"reopened": true`, and a WARNING-level `agent_reopened` log
    line marks the reopen for operator-side visibility. The versioned billing
    batch-recovery
    action (`billing_recovery=True`) is the one explicit caller that must not
    reopen: it refuses a closed row and any non-billing-class halt
    (`ResurrectRefused`), and marks the resurrect event payload with
    `via='billing_recovery'`. The host resumes the existing checkpoint after
    the transaction commits.
    """
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    reopened = _prepare_resurrect_attempt(
        agent_id,
        resurrected_by=resurrected_by,
        prompt=prompt,
        trigger_inbound_id=trigger_inbound_id,
        trigger_inbound_kind=trigger_inbound_kind,
        billing_recovery=billing_recovery,
    )
    payload: dict[str, object] = {"prompt": prompt} if prompt else {}
    if reopened:
        payload["reopened"] = True
        # LOUD operator-side audit: clearing the durable closure marker must be
        # findable without reading the resurrect event's payload — one distinct
        # WARNING line rides the existing log/event pipelines (no new surface).
        logger.warning(
            "closed agent {agent_id} reopened by explicit resurrect ({resurrected_by})",
            event="agent_reopened",
            agent_id=agent_id,
            resurrected_by=resurrected_by,
        )
    if billing_recovery:
        payload["via"] = "billing_recovery"
    insert_event_log(
        event_type="resurrect",
        agent_id=agent_id,
        source=resurrected_by,
        target_agent_id=_resurrect_event_target(resurrected_by),
        payload=payload,
    )
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by}",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
    )
    return agent_id
