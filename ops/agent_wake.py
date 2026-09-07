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
    """Run the one final resurrection CAS with a fully static SQL shape."""
    base_params = (AgentStatus.IDLING, agent_id, AgentStatus.TERMINATED)
    if trigger_inbound_id is not None:
        from shared.lifecycle_acceptance import FAILED_RESTART_FOR_CURRENT_TARGET

        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL, "
                "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL, "
                "runtime_protocol_version = 0 "
                "WHERE id = %s AND status = %s "
                "AND NOT {} AND EXISTS ("
                "  SELECT 1 FROM inbound_messages m "
                "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
                "    AND m.status = 'pending' AND m.kind = %s "
                "    AND m.created_at > agents_meta.status_changed_at "
                "    AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0)"
                ") RETURNING status_changed_at"
            ).format(sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET)),
            (*base_params, trigger_inbound_id, trigger_inbound_kind),
        )
    else:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, lease_expires_at = NULL, "
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
            "termination; UPDATE affected 0 rows"
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
) -> None:
    """Commit resurrection and its optional prompt before waking the host."""
    from shared.envelope import reject_unnegotiated_caller
    from shared.exec_owner_recovery import recover_local_resources

    reject_unnegotiated_caller(resurrected_by)
    recover_local_resources(agent_id, machine_name())
    with write_transaction() as conn, conn.cursor() as cur:
        latched_machine = _lock_active_home_machine(cur, agent_id)
        cur.execute("SELECT status,machine FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
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
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', %s)",
            (agent_id, resurrected_by),
        )
        if prompt is not None:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'chat', %s)",
                (agent_id, prompt, resurrected_by),
            )
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    publish_inbound_wake(agent_id, "0")


def resurrect_agent(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None,
) -> int:
    """Atomically restore native intent and enqueue lifecycle plus optional chat.

    Pending-work callers name the exact post-termination inbound. Its ID and
    the latest force-termination fence are checked under the metadata row lock.
    The host resumes the existing checkpoint after the transaction commits.
    """
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    _prepare_resurrect_attempt(
        agent_id,
        resurrected_by=resurrected_by,
        prompt=prompt,
        trigger_inbound_id=trigger_inbound_id,
        trigger_inbound_kind=trigger_inbound_kind,
    )
    insert_event_log(
        event_type="resurrect",
        agent_id=agent_id,
        source=resurrected_by,
        target_agent_id=_resurrect_event_target(resurrected_by),
        payload={"prompt": prompt} if prompt else {},
    )
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by}",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
    )
    return agent_id
