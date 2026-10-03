"""Durable termination messages and force fences. Resource settlement is owned by the original agent host."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents import AgentNotFound, AgentStatus
from base.agents.incarnation.lifecycle_acceptance import (
    HOSTED_TURN_RECOVERY_MARKER,
    KILL_ALL_SHELL_SESSIONS,
)
from base.agents.messages.envelope import validate_writable_source
from base.db import Database, publish_inbound_wake
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops.lifecycle.events import publish_page_closed as publish_page_closed
from ops.pages import list_open_page_names


def _insert_termination_pair(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> tuple[int | None, int]:
    """Insert an optional pending chat followed by its terminate command.

    `kill_all_shell_sessions` rides the command's payload
    (`base.agents.incarnation.lifecycle_acceptance.KILL_ALL_SHELL_SESSIONS`), so the request is
    durable in the same statement as the termination it accompanies.
    """
    message_id: int | None = None
    with conn.cursor() as cur:
        if message is not None:
            validate_writable_source(source)
            cur.execute(
                "INSERT INTO inbound_messages (agent_id,content,kind,source) "
                "VALUES (%s,%s,'chat',%s) RETURNING id",
                (agent_id, message, source),
            )
            message_row = cur.fetchone()
            if message_row is None:
                raise RuntimeError("termination message INSERT returned no id")
            message_id = int(message_row[0])
        cur.execute(
            "INSERT INTO inbound_messages (agent_id,content,kind,source,payload) "
            "VALUES (%s,'','terminate',%s,%s) RETURNING id",
            (
                agent_id,
                source,
                Jsonb({KILL_ALL_SHELL_SESSIONS: True}) if kill_all_shell_sessions else None,
            ),
        )
        terminate_row = cur.fetchone()
        if terminate_row is None:
            raise RuntimeError("terminate inbound INSERT returned no id")
    return message_id, int(terminate_row[0])


def _insert_pending_termination_message(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str,
) -> int:
    """Retry only the final chat after the termination command is durable."""
    validate_writable_source(source)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id,content,kind,source) "
            "VALUES (%s,%s,'chat',%s) RETURNING id",
            (agent_id, message, source),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("termination message retry INSERT returned no id")
        return int(row[0])


def _insert_recovery_wake(conn: psycopg.Connection, agent_id: int, content: str) -> None:
    """Queue the system chat that resurrects a force-terminated agent.

    Called inside the force transaction, after the fence is installed, so a
    committed force can never exist without its wake (the corpse reaper's
    `_queue_recovery_wake` does the same for a crash death). Two predicates of
    the resurrection trigger decide the shape of this row:

    - its id is above `last_force_terminate_inbound_id`, so it is inserted
      after the terminate command (any chat queued before the command is
      fenced out of resurrecting the agent);
    - its `created_at` is strictly after `status_changed_at`, which the status
      UPDATE just stamped with this transaction's `now()` — so the wake
      carries `clock_timestamp()`, never the transaction-start default.

    The `hosted_turn_recovery` marker is what lets a system-source chat
    resurrect (`HOSTED_TURN_RECOVERY_MARKER`).
    """
    conn.execute(
        "INSERT INTO inbound_messages (agent_id,content,kind,source,payload,created_at) "
        "VALUES (%s,%s,'chat','system',%s,clock_timestamp())",
        (agent_id, content, Jsonb({HOSTED_TURN_RECOVERY_MARKER: True})),
    )


def _insert_termination_inbounds(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> tuple[int | None, int]:
    """Insert termination inbounds, preserving terminate on message failure.

    The caller owns the outer transaction. Nested transactions are savepoints:
    the first keeps the chat and command atomic, while the second contains a
    failed best-effort chat retry without rolling back the durable command.
    """
    kill = kill_all_shell_sessions
    if message is None:
        return _insert_termination_pair(
            conn, agent_id, source=source, message=None, kill_all_shell_sessions=kill
        )
    try:
        with conn.transaction():
            return _insert_termination_pair(
                conn, agent_id, source=source, message=message, kill_all_shell_sessions=kill
            )
    except Exception as exc:
        logger.warning(
            "atomic termination message enqueue failed for agent {agent_id}; "
            "retrying the terminate command alone ({exc!r})",
            agent_id=agent_id,
            exc=exc,
        )
    _, terminate_id = _insert_termination_pair(
        conn, agent_id, source=source, message=None, kill_all_shell_sessions=kill
    )
    message_id: int | None = None
    try:
        with conn.transaction():
            message_id = _insert_pending_termination_message(
                conn,
                agent_id,
                source=source,
                message=message,
            )
    except Exception as exc:
        logger.warning(
            "termination message retry failed for agent {agent_id}; the terminate "
            "command remains durable ({exc!r})",
            agent_id=agent_id,
            exc=exc,
        )
    return message_id, terminate_id


def _stage_termination_event(
    conn: psycopg.Connection,
    *,
    agent_id: int,
    source: str,
    inbound_id: int,
    kill_all_shell_sessions: bool,
) -> telemetry.Event:
    """Register a terminate audit fact before its operation commits.

    A requested shell-session kill is named in the payload next to the command
    it accompanies.
    """
    payload: dict[str, object] = {"inbound_id": inbound_id}
    if kill_all_shell_sessions:
        payload[KILL_ALL_SHELL_SESSIONS] = True
    event = prepare_event_log(
        event_type="terminate", agent_id=agent_id, source=source, payload=payload
    )
    from base.agents.impersonation_manifest import record_central_event

    return record_audit(conn, record_central_event(conn, event))


def _enqueue_termination_inbounds(
    db: Database,
    bus: EventBus,
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> int | None:
    """Persist graceful termination and publish its audit/wake effects.

    With `kill_all_shell_sessions` the agent row is locked first and the
    request rides the terminate command, so it serializes against the home
    runtime's apply (which locks the same row): either the apply sees the
    request and kills the sessions before the termination commits, or this
    transaction sees the row already terminated and returns None — the caller
    then kills the sessions itself instead of queueing a request no apply
    will ever read. Without the option the path is unchanged.
    """
    with write_transaction(db_pool) as conn:
        if kill_all_shell_sessions:
            row = conn.execute(
                "SELECT status FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,)
            ).fetchone()
            if row is None:
                raise AgentNotFound(f"agent {agent_id} does not exist")
            if AgentStatus(row[0]) is AgentStatus.TERMINATED:
                return None
        _, terminate_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_id,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
    telemetry.emit_prepared(prepared_event)
    _publish_force_terminate_inbound(db, bus, agent_id, terminate_id, source)
    return terminate_id


def _force_terminate_transaction(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None = None,
    kill_all_shell_sessions: bool = False,
    recovery_wake: str | None = None,
) -> tuple[AgentStatus, int | None, list[str], int]:
    """Lock the agent, insert termination intent and install its host resource fence. A newer inbound cannot bypass this accepted force command.

    `recovery_wake` (the delivery watchdog's wedged-turn recovery) is the
    content of a marked system chat committed in this same transaction, after
    the fence (`_insert_recovery_wake`): the terminated agent then always has
    the pending trigger its resurrection retry works from, and a failed insert
    rolls the whole force back, leaving the agent as it was.

    `kill_all_shell_sessions` is recorded on the force command and in its audit
    event; the caller kills the sessions once this fence commits, and the host
    sweeps them again when it observes the force quiescent
    (`base.agents.incarnation.hosted_force`). The fence supersedes any unapplied graceful
    terminate, including a shell-session kill that terminate carried: a force
    kills sessions only when asked itself.
    """
    with db_pool.connection() as conn, conn.cursor() as cur:
        conn.execute("SET TRANSACTION READ WRITE")
        cur.execute(
            "SELECT status, pid FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        old_status = AgentStatus(row[0])
        pid = row[1]
        page_names = list_open_page_names(conn, agent_id)
        _, terminate_inbound_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        from base.agents.incarnation.lifecycle_acceptance import record_unowned_termination

        # Judged on the row as it is before this force ends it: no incarnation
        # to settle leaves a receipt resurrection accepts.
        record_unowned_termination(conn, agent_id, terminate_inbound_id)
        cur.execute(
            # termination_source='user': force-kill / a terminate that found the
            # pid already dead. Both are the user's will to end the agent, so it
            # is NOT crash-auto-resurrect-eligible even with a queued inbound.
            "UPDATE agents_meta SET status='terminated', termination_source='user', "
            "heartbeat_paused_until = NULL, last_force_terminate_inbound_id = %s "
            "WHERE id = %s",
            (terminate_inbound_id, agent_id),
        )
        from base.agents.incarnation.lifecycle_acceptance import supersede_lifecycle_for_force

        supersede_lifecycle_for_force(conn, agent_id, terminate_inbound_id)
        from base.agents.incarnation.hosted_force import install_hosted_force

        install_hosted_force(conn, agent_id, terminate_inbound_id)
        if recovery_wake is not None:
            _insert_recovery_wake(conn, agent_id, recovery_wake)
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_inbound_id,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
    telemetry.emit_prepared(prepared_event)
    return old_status, pid, page_names, terminate_inbound_id


def _publish_force_terminate_inbound(
    db: Database, bus: EventBus, agent_id: int, inbound_id: int, _source: str
) -> None:
    """Publish the non-transactional wake after the fenced audit commit."""
    publish_inbound_wake(db, bus, agent_id, str(inbound_id))


def force_mark_terminated(
    db: Database,
    bus: EventBus,
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str = "user",
    message: str | None = None,
) -> list[str]:
    """Install a force fence and return the affected page names."""
    _, _, page_names, inbound_id = _force_terminate_transaction(
        agent_id,
        db_pool,
        source=source,
        message=message,
    )
    _publish_force_terminate_inbound(db, bus, agent_id, inbound_id, source)
    return page_names
