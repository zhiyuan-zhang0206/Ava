"""PostgreSQL-backed browser sessions with short positive-result caching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_CACHE_TTL = timedelta(seconds=30)

# session id -> (cache deadline, authoritative row expiry)
_session_cache: dict[str, tuple[datetime, datetime]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def create_session(
    pool: ConnectionPool[Any],
    session_id: str,
    ttl_seconds: int,
    user_agent: str,
    ip: str,
) -> None:
    """Insert one session and opportunistically remove expired rows."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO web_sessions (id, expires_at, user_agent, ip)
            VALUES (%s, now() + make_interval(secs => %s), %s, %s)
            """,
            (session_id, ttl_seconds, user_agent, ip),
        )
        cur.execute("DELETE FROM web_sessions WHERE expires_at < now()")
    _session_cache.pop(session_id, None)


def session_is_valid(
    pool: ConnectionPool[Any],
    session_id: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether an opaque session exists, is unrevoked, and has not expired."""
    if not session_id:
        return False
    checked_at = now if now is not None else _now()
    cached = _session_cache.get(session_id)
    if cached is not None:
        cache_deadline, expires_at = cached
        if checked_at < cache_deadline and checked_at < expires_at:
            return True
        _session_cache.pop(session_id, None)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, expires_at, revoked_at FROM web_sessions WHERE id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    if row is None:
        return False
    _, expires_at, revoked_at = row
    if revoked_at is not None or expires_at <= checked_at:
        return False

    _session_cache[session_id] = (min(checked_at + _CACHE_TTL, expires_at), expires_at)
    return True


def revoke_session(pool: ConnectionPool[Any], session_id: str) -> bool:
    """Revoke one active session and evict its positive cache entry."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE web_sessions
            SET revoked_at = now()
            WHERE id = %s AND revoked_at IS NULL AND expires_at > now()
            RETURNING id
            """,
            (session_id,),
        )
        revoked = cur.fetchone() is not None
    _session_cache.pop(session_id, None)
    return revoked


def list_sessions(
    pool: ConnectionPool[Any],
    *,
    exclude_revoked: bool = True,
) -> list[dict[str, Any]]:
    """Return sessions newest-first; by default only currently active rows."""
    query = (
        """
        SELECT id, created_at, expires_at, revoked_at, last_seen_at, user_agent, ip
        FROM web_sessions
        WHERE revoked_at IS NULL AND expires_at > now()
        ORDER BY created_at DESC, id DESC
        """
        if exclude_revoked
        else """
        SELECT id, created_at, expires_at, revoked_at, last_seen_at, user_agent, ip
        FROM web_sessions
        ORDER BY created_at DESC, id DESC
        """
    )
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query)
        return [dict(row) for row in cur.fetchall()]


def touch_session(pool: ConnectionPool[Any], session_id: str) -> None:
    """Record recent use of a session."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE web_sessions SET last_seen_at = now() WHERE id = %s",
            (session_id,),
        )
