"""Durable termination messages and force fences. Resource settlement is owned by the original agent host."""

from __future__ import annotations

import psycopg
from psycopg_pool import ConnectionPool

from ops.ops_events import publish_page_closed as publish_page_closed
from ops.pages import list_open_page_names
from shared.agents import AgentNotFound, AgentStatus
from shared.audit_events import insert_event_log
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.envelope import validate_writable_source
from shared.log import logger


def _insert_termination_pair(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
) -> tuple[int | None, int]:
    """Insert an optional pending chat followed by its terminate command."""
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
            "INSERT INTO inbound_messages (agent_id,content,kind,source) "
            "VALUES (%s,'','terminate',%s) RETURNING id",
            (agent_id, source),
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


def _insert_termination_inbounds(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
) -> tuple[int | None, int]:
    """Insert termination inbounds, preserving terminate on message failure.

    The caller owns the outer transaction. Nested transactions are savepoints:
    the first keeps the chat and command atomic, while the second contains a
    failed best-effort chat retry without rolling back the durable command.
    """
    if message is None:
        return _insert_termination_pair(conn, agent_id, source=source, message=None)
    try:
        with conn.transaction():
            return _insert_termination_pair(conn, agent_id, source=source, message=message)
    except Exception as exc:
        logger.warning(
            "atomic termination message enqueue failed for agent {agent_id}; "
            "retrying the terminate command alone ({exc!r})",
            agent_id=agent_id,
            exc=exc,
        )
    _, terminate_id = _insert_termination_pair(conn, agent_id, source=source, message=None)
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


def _enqueue_termination_inbounds(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None,
) -> int:
    """Persist graceful termination and publish its audit/wake effects."""
    with write_transaction(db_pool) as conn:
        _, terminate_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
        )
    _publish_force_terminate_inbound(agent_id, terminate_id, source)
    return terminate_id


def _force_terminate_transaction(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None = None,
) -> tuple[AgentStatus, int | None, list[str], int]:
    """Lock the agent, insert termination intent and install its host resource fence. A newer inbound cannot bypass this accepted force command."""
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
        )
        cur.execute(
            # termination_source='user': force-kill / a terminate that found the
            # pid already dead. Both are the user's will to end the agent, so it
            # is NOT crash-auto-resurrect-eligible even with a queued inbound.
            "UPDATE agents_meta SET status='terminated', termination_source='user', "
            "heartbeat_paused_until = NULL, last_force_terminate_inbound_id = %s "
            "WHERE id = %s",
            (terminate_inbound_id, agent_id),
        )
        from shared.lifecycle_acceptance import supersede_lifecycle_for_force

        supersede_lifecycle_for_force(conn, agent_id, terminate_inbound_id)
        from shared.hosted_force import install_hosted_force

        install_hosted_force(conn, agent_id, terminate_inbound_id)
    return old_status, pid, page_names, terminate_inbound_id


def _publish_force_terminate_inbound(agent_id: int, inbound_id: int, source: str) -> None:
    """Emit the non-transactional audit/wake side effects after fence commit."""
    insert_event_log(
        event_type="terminate",
        agent_id=agent_id,
        source=source,
        payload={"inbound_id": inbound_id},
    )
    publish_inbound_wake(agent_id, str(inbound_id))


def _force_mark_terminated(
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
    _publish_force_terminate_inbound(agent_id, inbound_id, source)
    return page_names
