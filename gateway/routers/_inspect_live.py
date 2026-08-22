"""Fresh database projections for the agent inspector endpoint."""

from __future__ import annotations

from typing import Any, Literal, NamedTuple, cast

from fastapi import HTTPException
from psycopg_pool import ConnectionPool

from shared.agent_snapshot import OpenNotice
from shared.db import NOTICE_FYI_TTL_DAYS


class InspectDbRows(NamedTuple):
    """Live agents_meta fields that must never ride the aggregate TTL."""

    machine: str
    status: Any
    last_active_at: Any
    spawned_at: Any
    started_at: Any
    paused_until: Any
    pending_inbound: bool
    config_overlay: dict[str, Any]
    liveness_state: Literal["online", "offline", "unknown"]
    last_probe_at: Any


def db_rows_blocking(pool: ConnectionPool[Any], agent_id: int) -> InspectDbRows:
    """Read agents_meta and the pending-inbound flag in one DB borrow."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, machine, status, last_active_at, "
            "       spawned_at, started_at, "
            "       CASE WHEN heartbeat_paused_until > now() THEN heartbeat_paused_until END, "
            "       liveness_state, last_probe_at "
            "FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND status = 'pending')",
            (agent_id,),
        )
        pending_row = cur.fetchone()
        assert pending_row is not None  # noqa: S101 — EXISTS always returns one row
        return InspectDbRows(
            machine=row[1],
            status=row[2],
            last_active_at=row[3],
            spawned_at=row[4],
            started_at=row[5],
            paused_until=row[6],
            pending_inbound=bool(pending_row[0]),
            config_overlay=row[0] if row[0] is not None else {},
            liveness_state=cast(
                Literal["online", "offline", "unknown"],
                row[7] if row[7] is not None else "unknown",
            ),
            last_probe_at=row[8],
        )


def notice_blocking(pool: ConnectionPool[Any], agent_id: int) -> OpenNotice | None:
    """Read the agent's single unexpired open notice, if one exists."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, content, priority, require_response, blocking, created_at "
            "FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL "
            "AND (require_response OR created_at > now() - make_interval(days => %s)) "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id, NOTICE_FYI_TTL_DAYS),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return OpenNotice(
        id=row[0],
        title=row[1],
        content=row[2],
        priority=row[3],
        require_response=row[4],
        blocking=row[5],
        created_at=row[6],
    )
