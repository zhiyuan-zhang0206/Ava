"""Durable termination messages and force fences. Resource settlement is owned by the original agent host."""

from __future__ import annotations

import psycopg
from psycopg_pool import ConnectionPool

from ops.ops_events import publish_page_closed as publish_page_closed
from ops.pages import list_open_page_names
from shared import telemetry
from shared.agents import AgentNotFound, AgentStatus
from shared.audit_events import prepare_event_log
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.envelope import validate_writable_source
from shared.live_announce import publish_agent_updated_sync
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


def _stamp_closed(conn: psycopg.Connection, agent_id: int) -> None:
    """Stamp the closure marker inside the caller's transaction (first close wins).

    Every terminate path carrying `final=true` runs this in the SAME
    transaction as its termination intent: closing is never separable from the
    end-of-life it accompanies, so a crash between acceptance and death cannot
    leave a death whose auto-resurrect guard is missing. The WHERE keeps the
    first closure time across repeated close requests.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET closed_at = now() WHERE id = %s AND closed_at IS NULL",
            (agent_id,),
        )


def _stage_termination_event(
    conn: psycopg.Connection,
    *,
    agent_id: int,
    source: str,
    inbound_id: int,
    closed: bool,
) -> telemetry.Event:
    """Register a terminate audit fact before its operation commits."""
    payload: dict[str, object] = {"inbound_id": inbound_id}
    if closed:
        payload["closed"] = True
    event = prepare_event_log(
        event_type="terminate", agent_id=agent_id, source=source, payload=payload
    )
    from shared.agents.impersonation_manifest import stage_central_expected_event

    return stage_central_expected_event(
        conn, event, origin_kind="ops_terminate", origin_id=inbound_id
    )


def _enqueue_termination_inbounds(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None,
    final: bool = False,
) -> int:
    """Persist graceful termination and publish its audit/wake effects.

    `final` stamps the closure marker (never auto-resurrect) in the same
    transaction as the terminate command.
    """
    with write_transaction(db_pool) as conn:
        _, terminate_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
        )
        if final:
            _stamp_closed(conn, agent_id)
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_id,
            closed=final,
        )
    telemetry.emit_prepared(prepared_event)
    _publish_force_terminate_inbound(agent_id, terminate_id, source, closed=final)
    return terminate_id


def _force_terminate_transaction(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None = None,
    final: bool = False,
) -> tuple[AgentStatus, int | None, list[str], int]:
    """Lock the agent, insert termination intent and install its host resource fence. A newer inbound cannot bypass this accepted force command. `final` additionally stamps the closure marker in this same transaction."""
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
        if final:
            _stamp_closed(conn, agent_id)
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_inbound_id,
            closed=final,
        )
    telemetry.emit_prepared(prepared_event)
    return old_status, pid, page_names, terminate_inbound_id


def _publish_force_terminate_inbound(
    agent_id: int, inbound_id: int, _source: str, *, closed: bool = False
) -> None:
    """Publish the non-transactional wake after the fenced audit commit.

    `closed` records a `final=true` termination in the same audit trail — the
    marker itself is written transactionally with the termination intent; this
    only names it next to the command it accompanied.
    """
    del closed  # The matching audit event was staged before the fence committed.
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


def mark_agent_closed(agent_id: int, *, source: str, db_pool: ConnectionPool) -> bool:
    """Close an already-terminated agent — the metadata-only `terminate --final`.

    On a dead agent there is no termination left to apply; the closure is the
    whole action — and the backfill route for agents closed before the marker
    existed. Writes the marker, records the `terminate` audit event with
    `{"closed": true}`, refreshes mounted frontends, and returns whether THIS
    call is what closed the agent (False = already closed: no duplicate event).

    Callers must have observed `terminated` first: this marks, it never ends a
    live agent.
    """
    with write_transaction(db_pool) as conn:
        row = conn.execute(
            "UPDATE agents_meta SET closed_at = now() WHERE id = %s AND closed_at IS NULL "
            "RETURNING id",
            (agent_id,),
        ).fetchone()
        if row is None:
            prepared_event = None
        else:
            event = prepare_event_log(
                event_type="terminate", agent_id=agent_id, source=source, payload={"closed": True}
            )
            from shared.agents.impersonation_manifest import stage_central_expected_event

            prepared_event = stage_central_expected_event(
                conn, event, origin_kind="ops_mark_closed", origin_id=agent_id
            )
    if row is None:
        return False
    if prepared_event is None:
        raise RuntimeError("closed termination event was not staged")
    telemetry.emit_prepared(prepared_event)
    publish_agent_updated_sync(agent_id)
    return True
