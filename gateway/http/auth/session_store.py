"""PostgreSQL-backed browser sessions with short positive-result caching.

A session id is `<mint>.<random>`: the mint names the credential that logged
the browser in (`gateway.http.auth.request_principal.login_mint`). Validity is the row
(unrevoked, unexpired) AND a mint the caller still admits, so a session never
outlives the credential that minted it.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from base.cluster.auth import new_session_id
from base.db.transaction import write_transaction

_CACHE_TTL = timedelta(seconds=30)
_SESSION_CACHE_MAX_ENTRIES = 4096


def _now() -> datetime:
    return datetime.now(UTC)


def minted_session_id(mint: str) -> str:
    """A new opaque session id bound to `mint` (256 random bits after it)."""
    return f"{mint}.{new_session_id()}"


def session_mint(session_id: str) -> str | None:
    """The mint a session id carries, or None for an id without one."""
    mint, dot, rest = session_id.partition(".")
    return mint if dot and mint and rest else None


class SessionStore:
    """The `web_sessions` rows behind one pool, with a short positive-result cache.

    One per gateway process (the app lifespan builds it onto `app.state.sessions`). The cache
    maps a session id to (cache deadline, authoritative row expiry); `cache` is a parameter
    so a caller can hand in the mapping it wants.
    """

    def __init__(
        self,
        pool: ConnectionPool[Any],
        *,
        max_cache_entries: int = _SESSION_CACHE_MAX_ENTRIES,
        cache: OrderedDict[str, tuple[datetime, datetime]] | None = None,
    ) -> None:
        self._pool = pool
        self._max_cache_entries = max_cache_entries
        self._cache: OrderedDict[str, tuple[datetime, datetime]] = (
            OrderedDict() if cache is None else cache
        )

    def create(self, session_id: str, ttl_seconds: int, user_agent: str, ip: str) -> None:
        """Insert one session and evict any stale positive cache entry."""
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO web_sessions (id, expires_at, user_agent, ip)
                VALUES (%s, now() + make_interval(secs => %s), %s, %s)
                """,
                (session_id, ttl_seconds, user_agent, ip),
            )
        self._cache.pop(session_id, None)

    def is_valid(
        self,
        session_id: str | None,
        *,
        admitted: Collection[str],
        now: datetime | None = None,
    ) -> bool:
        """Whether a session carries an `admitted` mint, exists, is unrevoked and
        has not expired. A mint no longer admitted is refused before the cache and
        the database are consulted."""
        if not session_id or session_mint(session_id) not in admitted:
            return False
        checked_at = now if now is not None else _now()
        cached = self._cache.get(session_id)
        if cached is not None:
            cache_deadline, expires_at = cached
            if checked_at < cache_deadline and checked_at < expires_at:
                with suppress(KeyError):
                    self._cache.move_to_end(session_id)
                return True
            self._cache.pop(session_id, None)

        with self._pool.connection() as conn, conn.cursor() as cur:
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

        self._cache[session_id] = (min(checked_at + _CACHE_TTL, expires_at), expires_at)
        with suppress(KeyError):
            self._cache.move_to_end(session_id)
        if len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)
        return True

    def revoke(self, session_id: str) -> bool:
        """Revoke one active session and evict its positive cache entry."""
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
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
        self._cache.pop(session_id, None)
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


class SessionTouchThrottle:
    """Per-session throttle for `touch_session`: one DB write per interval.

    Owned by the gateway app (`app.state.session_touch`), so the bookkeeping
    lives and dies with the process serving the requests. `last_touch` maps a
    session id to the monotonic time it was last touched.
    """

    INTERVAL_S = 60.0
    MAX_ENTRIES = 1024

    def __init__(self) -> None:
        self.last_touch: dict[str, float] = {}

    def due(self, session_id: str, now: float, *, stale_after_s: float) -> bool:
        """Whether `session_id` should be touched now; records the touch when so."""
        last = self.last_touch.get(session_id)
        touch_due = last is None or now - last >= self.INTERVAL_S
        if touch_due:
            self.last_touch[session_id] = now
        self._prune(now, stale_after_s)
        return touch_due

    def _prune(self, now: float, stale_after_s: float) -> None:
        """Drop expired bookkeeping and cap the map by oldest touch time."""
        if len(self.last_touch) <= self.MAX_ENTRIES:
            return
        stale_before = now - stale_after_s
        for session_id, touched_at in tuple(self.last_touch.items()):
            if touched_at < stale_before:
                self.last_touch.pop(session_id, None)
        overflow = len(self.last_touch) - self.MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(self.last_touch, key=self.last_touch.__getitem__)[:overflow]
            for session_id in oldest:
                self.last_touch.pop(session_id, None)


def touch_session(pool: ConnectionPool[Any], session_id: str) -> None:
    """Record recent use of a session."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE web_sessions SET last_seen_at = now() WHERE id = %s",
            (session_id,),
        )


def session_ids_with_suffix(pool: ConnectionPool[Any], suffix: str) -> list[str]:
    """Active (unrevoked, unexpired) session ids ending with ``suffix``, newest first.

    The sessions list masks non-current ids to their final 8 characters; the
    revoke endpoint accepts that suffix, so this lookup resolves it back to a
    row. `right()` avoids LIKE wildcards entirely (ids are base64url, which
    includes ``_``).
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM web_sessions
            WHERE revoked_at IS NULL AND expires_at > now()
              AND right(id, %s) = %s
            ORDER BY created_at DESC, id DESC
            """,
            (len(suffix), suffix),
        )
        return [row[0] for row in cur.fetchall()]
