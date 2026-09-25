"""Public agent-scoped session handles over the durable impersonation store."""

from typing import Any

from shared.agents import impersonation as control
from shared.agents.impersonation.impersonation_history import public_session, resolve
from shared.caller_identity import CallerIdentity
from shared.db import connect


def request(
    agent_id: int,
    *,
    name: str,
    executor_name: str,
    provider: str,
    process_metadata: dict[str, Any],
    ttl_seconds: int = 3600,
    reason: str = "",
    thread_id: str | None = None,
    codex_remote: str | None = None,
    batch_window_seconds: int = 0,
) -> dict[str, Any]:
    """Request an automatic safe-boundary takeover; return credentials once.

    ``batch_window_seconds`` is the relay's routine-message merge window (see
    ``control.request``): 0, the default, delivers routine arrivals immediately;
    1..300 coalesces them into one hint per window while user chat and cancel
    always hint immediately (validated 0..300; task #3696 exception inventory).
    """
    result = control.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject=provider),
        name=name,
        executor_name=executor_name,
        process_metadata=process_metadata,
        automatic=True,
        ttl_seconds=ttl_seconds,
        reason=reason,
        relay_provider=provider,
        relay_thread_id=thread_id,
        relay_codex_remote=codex_remote,
        relay_batch_window_seconds=batch_window_seconds,
    )
    if "relay_token" in result:
        return public_session(result) | {"relay_token": result["relay_token"]}
    return public_session(result)


def list_sessions(
    agent_id: int, *, before: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Page permanent history newest first; IDs are never reused.

    ``limit`` is one page (default 100, validated 1..1000); pass ``before``
    to read rows older than that session id.
    """
    from psycopg.rows import dict_row

    if not 1 <= limit <= 1000:
        raise ValueError("limit must be from 1 through 1000")
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE agent_id=%s "
            "AND (%s::bigint IS NULL OR session_id<%s) ORDER BY session_id DESC LIMIT %s",
            (agent_id, before, before, limit),
        )
        return [public_session(row) for row in cur.fetchall()]


def private_id(agent_id: int, session_id: int) -> str:
    """Resolve one exact agent/session pair; no global integer lookup."""
    return str(resolve(agent_id, session_id)["id"])
