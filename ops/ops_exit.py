"""Terminate fence + exit-finalize ops — the kill transaction and its finalizers.

Split out of `ops/ops_lifecycle.py` (Task #1999) when the lifecycle cluster
crossed the 800-line ceiling. Owns the durable half of a force-terminate: the
fence transaction (terminate inbound as the monotonic intent fence, the
terminated flip, the optional process kill while the row lock is held), and
the exit finalizer the agent / gateway call when a process reports it
reached its own exit (`mark_agent_exited_op`).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from ops.agent_identity import AgentProcessIdentity, probe_agent_process
from ops.ops_events import publish_page_closed as publish_page_closed
from ops.pages import list_open_page_names
from shared.agents import AgentNotFound, AgentStatus
from shared.audit_events import insert_event_log
from shared.cluster import session_name
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.envelope import validate_writable_source
from shared.live_announce import publish_agent_updated_sync
from shared.log import logger
from shared.proc import force_kill
from shared.session_backend import native_proc


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
    kill_process: bool,
    message: str | None = None,
) -> tuple[AgentStatus, int | None, list[str], int]:
    """Fence and mark one explicit kill while holding the agent row lock.

    The newly inserted terminate inbound id is the monotonic intent fence. A
    guarded chat resurrection competing for this row either commits first (and
    this kill follows it) or waits, then observes a fence greater than its
    trigger. When a process should be killed, keep the row lock through the OS
    kill so a post-force chat cannot create a new session in the gap between DB
    intent and session cleanup. The transaction commits on context exit.
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
        if kill_process:
            agent_session = session_name(f"agent-{agent_id}")
            native_proc().kill_session(agent_session, graceful=False)
            # The exact session name is safe to clear repeatedly. A raw
            # pid can be recycled even while a stale row still says live,
            # so only positive argv identity evidence licenses SIGKILL.
            if (
                pid is not None
                and old_status is not AgentStatus.TERMINATED
                and probe_agent_process(pid, agent_id) is AgentProcessIdentity.OWNED
            ):
                force_kill(pid)
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
    """Fence an explicit zombie kill and return cascade-closed page names."""
    _, _, page_names, inbound_id = _force_terminate_transaction(
        agent_id,
        db_pool,
        source=source,
        kill_process=False,
        message=message,
    )
    _publish_force_terminate_inbound(agent_id, inbound_id, source)
    return page_names


async def mark_agent_exited_op(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    generation: UUID | None = None,
    owner: UUID | None = None,
) -> list[str]:
    """Finalize a self-exiting agent process: guarded status flip + events.

    Match the original generation and owner, not merely the agent id. Missing
    tokens can match only unknown legacy rows. Process ownership also requires
    a non-NULL pid: a handed-off unclaimed successor must not be finalized.

    POST /api/agents/{id}/exited calls this when an agent reaches its own
    process-exit finally block (graceful terminate or silent death from a
    SIGHUP/SIGTERM). The agent used to do this inline; it now notifies the
    gateway so it never writes agents_meta or reads the pages table itself.

    Guarded `WHERE status IN ('running','idling')` — the same
    invariant the inline version held:
    - 'restarting' is left untouched (reserved for the restarter daemon; a
      restart goes claim -> 'restarting' -> END -> process exit -> this call,
      and clobbering it to 'terminated' would strand the restart).
    - an unclaimed 'idling' row has no process to send this callback; a normal
      pre-claim death is instead handled by the dead-birth reaper.
    - already 'terminated' -> rowcount 0, idempotent (finally may run twice).

    Differs from `_force_mark_terminated` (used by the force-kill / zombie-reap
    paths), which overwrites status unconditionally — that is wrong here
    because the self-exit path must respect an in-flight restart.

    rowcount==1: really transitioned — publish AgentUpdated + the
    agent_terminated event + PageClosed for each cascade-closed page; returns
    those page names.
    rowcount==0: benign skip; debug-log the actual status to tell
    restarting / already-terminated / idling apart.
    other: the PK guarantees <=1 row; anything else is table corruption -> raise.
    """
    rowcount, page_names, actual_status = await asyncio.to_thread(
        _mark_exited_blocking, agent_id, db_pool, generation, owner
    )
    if rowcount == 1:
        logger.info(
            "agent {agent_id} terminated",
            event="agent_terminated",
            agent_id=agent_id,
        )
        for page_name in page_names:
            await publish_page_closed(agent_id, page_name)
        return page_names
    if rowcount == 0:
        logger.debug(
            "agent {agent_id} mark_agent_exited_op noop (actual status={actual_status!r})",
            agent_id=agent_id,
            actual_status=actual_status,
        )
        return []
    raise RuntimeError(
        f"mark_agent_exited_op agent_id={agent_id} rowcount={rowcount} — "
        f"PK invariant violated; agents_meta table integrity broken"
    )


def _mark_exited_blocking(
    agent_id: int,
    db_pool: ConnectionPool,
    generation: UUID | None = None,
    owner: UUID | None = None,
) -> tuple[int, list[str], str | None]:
    """Sync DB section of mark_agent_exited_op — via to_thread (the status flip,
    the exit event_log row, the AgentUpdated publish). Returns
    (rowcount, page_names, actual_status)."""
    with write_transaction(db_pool) as conn:
        # SELECT cascade-closable show() pages before UPDATE; daemon-supervised
        # serve() pages stay open and must not emit PageClosed.
        page_names = list_open_page_names(conn, agent_id)
        with conn.cursor() as cur:
            cur.execute(
                # termination_source='exit': the agent's own graceful process-exit
                # finalize (self-terminate, or a caught SIGTERM/SIGHUP that ran the
                # exit finally). Intentional — NOT crash-auto-resurrect-eligible. A
                # SIGKILL/OOM leaves no finally, so it never reaches here; the reaper
                # catches that dead pid and stamps 'reaper' instead.
                "UPDATE agents_meta SET status = %s, termination_source = 'exit', "
                "heartbeat_paused_until = NULL, lease_expires_at = NULL "
                "WHERE id = %s AND status IN (%s, %s) "
                "AND runtime_generation IS NOT DISTINCT FROM %s "
                "AND runtime_owner IS NOT DISTINCT FROM %s "
                "AND (runtime_kind IS NULL OR runtime_kind = 'hosted' "
                "OR (runtime_kind = 'process' AND pid IS NOT NULL))",
                (
                    AgentStatus.TERMINATED,
                    agent_id,
                    AgentStatus.RUNNING,
                    AgentStatus.IDLING,
                    generation,
                    owner,
                ),
            )
            rowcount = cur.rowcount
            if rowcount == 1:
                from shared.audit_events import insert_event_log

                insert_event_log(
                    event_type="exit",
                    agent_id=agent_id,
                    source="system",
                )
            actual_status: str | None = None
            if rowcount == 0:
                cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
                row = cur.fetchone()
                actual_status = row[0] if row is not None else None
    if rowcount == 1:
        with db_pool.connection() as conn:
            publish_agent_updated_sync(conn, agent_id)
    return rowcount, page_names, actual_status


# ─── spawn ─────────────────────────────────────────────────────────────────
