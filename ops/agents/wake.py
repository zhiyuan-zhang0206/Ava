"""Resume a terminated agent by preserving its identity and enqueuing a wake."""

from datetime import datetime
from typing import Literal, LiteralString

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from base import telemetry
from base.agents import (
    AgentNotFound,
    AgentStatus,
    MachinePaused,
    ResurrectAlreadyAlive,
    ResurrectBudgetExhausted,
    ResurrectRefused,
)
from base.agents.incarnation.lifecycle_acceptance import (
    LIFECYCLE_RELEASE,
    UNOWNED_TERMINATION_ID,
    UNOWNED_TERMINATION_RECORDED,
)
from base.cluster.machine import machine_name
from base.config import field_alias, get_field, settings
from base.db import fetch_one, publish_inbound_wake
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry.audit_events import prepare_event_log
from ops.agents.resurrection_retry import (
    ResurrectSettlementDeferredError,
    hosted_resurrection_target,
)
from ops.agents.resurrection_retry import ResurrectTriggerStaleError as ResurrectTriggerStaleError
from ops.agents.resurrection_retry import lock_active_home_machine as _lock_active_home_machine

# The exact retained hosted identity; or, all three NULL, a never-admitted row
# whose fresh-INSERT birth marker is still unconsumed, or a row whose unowned
# termination receipt the last parameter names.
_RESURRECTION_TARGET: LiteralString = (
    "runtime_kind IS NOT DISTINCT FROM %s AND runtime_generation IS NOT DISTINCT FROM %s "
    "AND runtime_owner IS NOT DISTINCT FROM %s "
    "AND (runtime_kind IS NOT NULL OR incarnation_resources->>'state'='unadmitted' OR "
    + UNOWNED_TERMINATION_RECORDED
    + ")"
)
# The locked row the gate judges, with this life's unowned termination receipt.
_RESURRECTION_ROW: LiteralString = (
    "SELECT status,machine,permanent_reject_streak,last_permanent_reject_reason,"  # noqa: S608 -- constant SQL fragment
    "runtime_kind,runtime_generation,runtime_owner,pid,incarnation_resources,"
    + UNOWNED_TERMINATION_ID
    + " FROM agents_meta WHERE id = %s FOR UPDATE"
)


def _transition_terminated_to_unclaimed_idling(
    cur: psycopg.Cursor,
    agent_id: int,
    incarnation: RuntimeIncarnation | None,
    *,
    unowned_termination: int | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None,
) -> datetime:
    """Run the one final resurrection CAS with a fully static SQL shape.

    `incarnation` None is a fresh hosted birth: the CAS re-proves, under the
    row lock, that no runtime identity exists and that either the birth marker
    is intact or `unowned_termination` names this agent's unowned force receipt.
    """
    base_params = (
        AgentStatus.IDLING,
        agent_id,
        AgentStatus.TERMINATED,
        None if incarnation is None else "hosted",
        None if incarnation is None else incarnation.generation,
        None if incarnation is None else incarnation.owner,
        unowned_termination,
    )
    if trigger_inbound_id is not None:
        from base.agents.incarnation.lifecycle_acceptance import (
            FAILED_RESTART_FOR_CURRENT_TARGET,
            SYSTEM_REAPED_CRASH_ROW,
        )
        from base.agents.recovery_breaker import RECOVERY_BREAKER_CLEAR

        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL, "
                "last_turn_fatal_at = NULL, "
                "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL, "
                "runtime_protocol_version = 0 "
                "WHERE id = %s AND status = %s AND {} "
                "AND pid IS NULL AND lifecycle_command_id IS NULL "
                "AND NOT {} "
                "AND (agents_meta.wake_suppressed_until IS NULL "
                "     OR agents_meta.wake_suppressed_until < now()) "
                "AND {} "
                "AND EXISTS ("
                "  SELECT 1 FROM inbound_messages m "
                "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
                "    AND m.status = 'pending' AND m.kind = %s "
                "    AND (m.created_at > agents_meta.status_changed_at OR {}) "
                "    AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0)"
                ") RETURNING status_changed_at"
            ).format(
                sql.SQL(_RESURRECTION_TARGET),
                sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET),
                sql.SQL(RECOVERY_BREAKER_CLEAR),
                sql.SQL(SYSTEM_REAPED_CRASH_ROW),
            ),
            (*base_params, trigger_inbound_id, trigger_inbound_kind),
        )
    else:
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL, "
                "last_turn_fatal_at = NULL, "
                "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL, "
                "runtime_protocol_version = 0 "
                "WHERE id = %s AND status = %s AND {} "
                "AND pid IS NULL AND lifecycle_command_id IS NULL RETURNING status_changed_at"
            ).format(sql.SQL(_RESURRECTION_TARGET)),
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
            "wakes, or a tripped recovery breaker)"
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
    from base.host.env.runtime_config import read_env_aliases

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
) -> telemetry.Event:
    """Commit resurrection and its optional prompt before waking the host.

    `billing_recovery=True` (the versioned `resurrect-billing-v1` action, task
    #3919) re-checks the billing-victim contract under the same row lock: a
    row that is not a billing-class recovery-breaker halt is refused
    (`ResurrectRefused`) — the batch entry only reinstates the recorded
    billing cohort.
    """
    from base.agents.incarnation.exec_owner_recovery import recover_local_resources
    from base.agents.incarnation.lifecycle_acceptance import supersede_lifecycle_for_resurrect
    from base.agents.messages.envelope import reject_unnegotiated_caller

    reject_unnegotiated_caller(resurrected_by)
    recover_local_resources(agent_id, machine_name())
    with write_transaction() as conn, conn.cursor() as cur:
        latched_machine = _lock_active_home_machine(cur, agent_id)
        cur.execute(_RESURRECTION_ROW, (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if row[1] != latched_machine:
            raise ResurrectTriggerStaleError("resurrection placement changed after pause latch")
        current = AgentStatus(row[0])
        if current is not AgentStatus.TERMINATED:
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} is in {current.value!r} state, not 'terminated'"
            )
        unowned_termination = row[9]
        incarnation = hosted_resurrection_target(
            agent_id,
            kind=row[4],
            generation=row[5],
            owner=row[6],
            pid=row[7],
            resources=row[8],
            unowned_termination=unowned_termination,
        )
        if billing_recovery:
            from base.agents.recovery_breaker import (
                HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
                PERMANENT_REJECT_REASON_BILLING,
            )

            if (
                row[2] < HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS
                or row[3] != PERMANENT_REJECT_REASON_BILLING
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
        # The resurrection inbound is inserted before the settlement check so
        # its id can fence the new incarnation's epoch: every earlier unapplied
        # lifecycle command is settled as superseded right here (issue #2158),
        # and a command that never applied cannot defer this resurrection. A
        # refusal below rolls the whole transaction back - fence included.
        # Stamp after the lock: a transaction begun before termination committed
        # must not sort its resurrection ahead of the interruption notices. The
        # row leaves this transaction unowned, a lifecycle release.
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at, payload) "
            "VALUES (%s, '', 'resurrect', %s, clock_timestamp(), %s) RETURNING id",
            (agent_id, resurrected_by, Jsonb({LIFECYCLE_RELEASE: True})),
        )
        resurrect_row = cur.fetchone()
        if resurrect_row is None:
            raise RuntimeError("resurrect lifecycle inbound INSERT returned no id")
        supersede_lifecycle_for_resurrect(conn, agent_id, resurrect_row[0])
        cur.execute("SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,))
        if fetch_one(cur, "resurrect: locked lifecycle pointer")[0] is not None:
            raise ResurrectSettlementDeferredError(
                "outstanding hosted lifecycle command has not settled"
            )
        _transition_terminated_to_unclaimed_idling(
            cur,
            agent_id,
            incarnation,
            unowned_termination=unowned_termination,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
        if prompt is not None:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at) "
                "VALUES (%s, %s, 'chat', %s, clock_timestamp())",
                (agent_id, prompt, resurrected_by),
            )
        prepared_event = _stage_resurrect_event(
            conn,
            agent_id,
            resurrected_by,
            prompt,
            billing_recovery=billing_recovery,
            origin_id=int(resurrect_row[0]),
        )
        conn.commit()
        publish_agent_updated_sync(agent_id)
    publish_inbound_wake(agent_id, "0")
    return prepared_event


def _stage_resurrect_event(
    conn: psycopg.Connection,
    agent_id: int,
    resurrected_by: str,
    prompt: str | None,
    *,
    billing_recovery: bool,
    origin_id: int,
) -> telemetry.Event:
    """Stage the exact resurrection audit fact inside the owning transaction."""
    payload: dict[str, object] = {"prompt": prompt} if prompt else {}
    if billing_recovery:
        payload["via"] = "billing_recovery"
    event = prepare_event_log(
        event_type="resurrect",
        agent_id=agent_id,
        source=resurrected_by,
        target_agent_id=_resurrect_event_target(resurrected_by),
        payload=payload,
    )
    from base.agents.impersonation_manifest import stage_central_expected_event

    return stage_central_expected_event(conn, event, origin_kind="agent_wake", origin_id=origin_id)


def resurrect_agent(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None,
    billing_recovery: bool = False,
) -> int:
    """Resume a terminated hosted incarnation and enqueue lifecycle plus optional chat.

    Historical process/unknown runtimes and incomplete hosted identities require
    explicit cutover reconciliation. Only the hosted lifecycle owner settles an
    applied command; resurrection never infers its completion from process exit.

    Pending-work callers name the exact post-termination inbound. Its ID and
    the latest force-termination fence are checked under the metadata row lock,
    and — for a row the system itself reaped after a crash
    (`SYSTEM_REAPED_CRASH_ROW`) — work that predates the termination still
    qualifies: a reaper death is not an operator's decision. The automatic
    trigger additionally requires clear automatic wakes (no suppression window,
    `RECOVERY_BREAKER_CLEAR`); explicit manual resurrection passes no trigger
    and keeps its unconditional contract. The versioned billing batch-recovery
    action (`billing_recovery=True`) refuses any non-billing-class halt
    (`ResurrectRefused`) and marks the resurrect event payload with
    `via='billing_recovery'`. The host resumes the existing checkpoint after
    the transaction commits.
    """
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    prepared_event = _prepare_resurrect_attempt(
        agent_id,
        resurrected_by=resurrected_by,
        prompt=prompt,
        trigger_inbound_id=trigger_inbound_id,
        trigger_inbound_kind=trigger_inbound_kind,
        billing_recovery=billing_recovery,
    )
    telemetry.emit_prepared(prepared_event)
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by}",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
    )
    return agent_id
