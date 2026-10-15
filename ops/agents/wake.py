"""Resume a terminated agent by preserving its identity and enqueuing a wake."""

from datetime import datetime
from typing import LiteralString

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from base import telemetry
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.incarnation.lifecycle_acceptance import (
    LIFECYCLE_RELEASE,
    UNOWNED_TERMINATION_ID,
    UNOWNED_TERMINATION_RECORDED,
)
from base.agents.messages.inbound import WakeTriggerKind, validate_wake_trigger_kind
from base.api_contracts.agent_contract import (
    AgentNotFound,
    AgentStatus,
    MachinePaused,
    ResurrectAlreadyAlive,
)
from base.db import Database, fetch_one
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import ava_home
from base.telemetry.audit_events import prepare_event_log, record_audit
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
RESURRECTION_ROW: LiteralString = (
    "SELECT status,machine,permanent_reject_streak,last_permanent_reject_reason,"  # noqa: S608 -- constant SQL fragment
    "runtime_kind,runtime_generation,runtime_owner,pid,incarnation_resources,"
    + UNOWNED_TERMINATION_ID
    + " FROM agents_meta WHERE id = %s FOR UPDATE"
)


def transition_terminated_to_unclaimed_idling(
    cur: psycopg.Cursor,
    agent_id: int,
    incarnation: RuntimeIncarnation | None,
    *,
    unowned_termination: int | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: WakeTriggerKind | None,
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
        from base.agents.recovery.breaker import RECOVERY_BREAKER_CLEAR

        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL, "
                "last_turn_fatal_at = NULL, "
                "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL "
                "WHERE id = %s AND status = %s AND {} "
                "AND pid IS NULL AND lifecycle_command_id IS NULL "
                "AND {} "
                "AND EXISTS ("
                "  SELECT 1 FROM inbound_messages m "
                "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
                "    AND m.status = 'pending' AND m.kind = %s "
                "    AND m.created_at > agents_meta.status_changed_at "
                "    AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0)"
                ") RETURNING status_changed_at"
            ).format(
                sql.SQL(_RESURRECTION_TARGET),
                sql.SQL("TRUE" if trigger_inbound_kind == "chat" else RECOVERY_BREAKER_CLEAR),
            ),
            (*base_params, trigger_inbound_id, trigger_inbound_kind),
        )
    else:
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL, "
                "last_turn_fatal_at = NULL, "
                "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL "
                "WHERE id = %s AND status = %s AND {} "
                "AND pid IS NULL AND lifecycle_command_id IS NULL RETURNING status_changed_at"
            ).format(sql.SQL(_RESURRECTION_TARGET)),
            base_params,
        )
    transition_row = cur.fetchone()
    if transition_row is not None:
        if trigger_inbound_kind == "chat":
            cur.execute(
                "UPDATE agents_meta SET permanent_reject_streak=0, "
                "last_permanent_reject_reason=NULL, wake_suppressed_until=NULL, "
                "wake_suppress_reason=NULL WHERE id=%s",
                (agent_id,),
            )
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


def _prepare_resurrect_attempt(
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: WakeTriggerKind | None,
    local_machine: str,
) -> telemetry.Event:
    """Commit ordinary resurrection and its optional prompt before waking the host."""
    from base.agents.incarnation.exec_owner_recovery import recover_local_resources
    from base.agents.incarnation.lifecycle_acceptance import supersede_lifecycle_for_resurrect

    recover_local_resources(db, agent_id, local_machine, home=ava_home())
    with db.write_transaction() as conn, conn.cursor() as cur:
        # Task before agent: producers hold the task while inserting an inbound
        # whose foreign key can lock agents_meta. Keep the same lock order.
        from base.agents.tasks.delivery import lock_task_note_owner

        if trigger_inbound_id is not None and not lock_task_note_owner(
            cur, agent_id, trigger_inbound_id
        ):
            raise ResurrectTriggerStaleError(
                f"agent {agent_id} task assignment is no longer current"
            )
        latched_machine = _lock_active_home_machine(cur, agent_id)
        cur.execute(RESURRECTION_ROW, (agent_id,))
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
        transition_terminated_to_unclaimed_idling(
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
        )
        conn.commit()
        publish_agent_updated_sync(bus, agent_id)
    publish_inbound_wake(db, bus, agent_id, "0")
    return prepared_event


def _stage_resurrect_event(
    conn: psycopg.Connection,
    agent_id: int,
    resurrected_by: str,
    prompt: str | None,
) -> telemetry.Event:
    """Stage the exact resurrection audit fact inside the owning transaction."""
    payload: dict[str, object] = {"prompt": prompt} if prompt else {}
    event = prepare_event_log(
        event_type="resurrect",
        agent_id=agent_id,
        source=resurrected_by,
        target_agent_id=_resurrect_event_target(resurrected_by),
        payload=payload,
    )
    from base.agents.impersonation.manifest import record_central_event

    return record_audit(conn, record_central_event(conn, event))


def resurrect_agent(
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
    local_machine: str,
) -> int:
    """Resume a terminated hosted incarnation for explicit or exact new input.

    Home-machine admission and resource fences remain required. Pending triggers
    must be newer than termination and above the force fence. A permanent provider
    error blocks background triggers; new chat clears that halt as an explicit
    attempt after the fix. Resurrection does not replay prior graph work."""
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    if trigger_inbound_kind is not None:
        trigger_inbound_kind = validate_wake_trigger_kind(trigger_inbound_kind)
    prepared_event = _prepare_resurrect_attempt(
        db,
        bus,
        agent_id,
        resurrected_by=resurrected_by,
        prompt=prompt,
        trigger_inbound_id=trigger_inbound_id,
        trigger_inbound_kind=trigger_inbound_kind,
        local_machine=local_machine,
    )
    telemetry.emit_prepared(prepared_event)
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by}",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
    )
    return agent_id
