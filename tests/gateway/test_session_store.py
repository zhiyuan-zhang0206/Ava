"""Contract tests for the PostgreSQL-backed browser session store."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from gateway import session_store
from gateway.session_store import (
    create_session,
    list_sessions,
    revoke_session,
    session_is_valid,
    touch_session,
)
from shared.config import settings


@pytest.fixture
def pool() -> Iterator[ConnectionPool[psycopg.Connection[Any]]]:
    session_pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=2,
        open=True,
    )
    try:
        yield session_pool
    finally:
        session_pool.close()


@pytest.fixture(autouse=True)
def _clear_session_cache() -> Iterator[None]:
    session_store._session_cache.clear()
    yield
    session_store._session_cache.clear()


def test_create_validate_revoke_lifecycle(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    create_session(pool, "session-one", 3600, "test-agent", "127.0.0.1")

    assert session_is_valid(pool, "session-one") is True
    assert revoke_session(pool, "session-one") is True
    assert session_is_valid(pool, "session-one") is False
    assert revoke_session(pool, "session-one") is False
    assert revoke_session(pool, "missing") is False


def test_revoke_expired_session_returns_false_without_marking_revoked(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-session",),
        )

    assert revoke_session(pool, "expired-session") is False
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT revoked_at FROM web_sessions WHERE id = %s",
            ("expired-session",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None


def test_cache_never_outlives_row_expiry(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    checked_at = datetime.now(UTC)
    expires_at = checked_at + timedelta(seconds=5)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, %s)",
            ("short-session", expires_at),
        )

    assert session_is_valid(pool, "short-session", now=checked_at) is True
    assert (
        session_is_valid(
            pool,
            "short-session",
            now=expires_at + timedelta(microseconds=1),
        )
        is False
    )


def test_valid_cache_hit_does_not_borrow_connection(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    checked_at = datetime.now(UTC)
    create_session(pool, "cached-session", 3600, "", "")
    assert session_is_valid(pool, "cached-session", now=checked_at) is True

    class FailingPool:
        def connection(self) -> None:
            raise AssertionError("cache hit borrowed a database connection")

    assert (
        session_is_valid(
            FailingPool(),  # type: ignore[arg-type]
            "cached-session",
            now=checked_at + timedelta(seconds=1),
        )
        is True
    )


def test_touch_advances_last_seen_at(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    create_session(pool, "touched-session", 3600, "", "")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE web_sessions SET last_seen_at = now() - interval '1 day' WHERE id = %s",
            ("touched-session",),
        )
        cur.execute("SELECT last_seen_at FROM web_sessions WHERE id = %s", ("touched-session",))
        before_row = cur.fetchone()
        assert before_row is not None
        before = before_row[0]

    touch_session(pool, "touched-session")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_seen_at FROM web_sessions WHERE id = %s", ("touched-session",))
        after_row = cur.fetchone()
        assert after_row is not None
        after = after_row[0]
    assert after > before


def test_list_sessions_filters_inactive_and_sorts_newest_first(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    now = datetime.now(UTC)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO web_sessions
                (id, created_at, expires_at, revoked_at, user_agent, ip)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    "older",
                    now - timedelta(minutes=2),
                    now + timedelta(hours=1),
                    None,
                    "ua-1",
                    "ip-1",
                ),
                (
                    "newer",
                    now - timedelta(minutes=1),
                    now + timedelta(hours=1),
                    None,
                    "ua-2",
                    "ip-2",
                ),
                ("revoked", now, now + timedelta(hours=1), now, "ua-3", "ip-3"),
                ("expired", now, now - timedelta(seconds=1), None, "ua-4", "ip-4"),
            ],
        )

    active = list_sessions(pool)
    all_sessions = list_sessions(pool, exclude_revoked=False)

    assert [row["id"] for row in active] == ["newer", "older"]
    assert active[0]["user_agent"] == "ua-2"
    assert active[0]["ip"] == "ip-2"
    assert {row["id"] for row in all_sessions} == {
        "older",
        "newer",
        "revoked",
        "expired",
    }
    revoked = next(row for row in all_sessions if row["id"] == "revoked")
    assert revoked["revoked_at"] is not None


def test_create_session_leaves_expired_rows_for_the_ttl_reaper(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-row",),
        )

    create_session(pool, "live-row", 3600, "", "")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM web_sessions ORDER BY id")
        assert [row[0] for row in cur.fetchall()] == ["expired-row", "live-row"]


def test_session_cache_evicts_least_recently_used_entry(
    pool: ConnectionPool[psycopg.Connection[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-lived gateway retains only the most recently used session cache entries."""
    monkeypatch.setattr(session_store, "_SESSION_CACHE_MAX_ENTRIES", 2)
    checked_at = datetime.now(UTC)
    for session_id in ("first", "second", "third"):
        create_session(pool, session_id, 3600, "", "")

    assert session_is_valid(pool, "first", now=checked_at) is True
    assert session_is_valid(pool, "second", now=checked_at) is True
    assert session_is_valid(pool, "first", now=checked_at + timedelta(seconds=1)) is True
    assert session_is_valid(pool, "third", now=checked_at) is True

    assert list(session_store._session_cache) == ["first", "third"]


def test_session_ids_with_suffix_matches_active_rows_only(
    pool: ConnectionPool[psycopg.Connection[Any]],
) -> None:
    now = datetime.now(UTC)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO web_sessions (id, created_at, expires_at, revoked_at)
            VALUES (%s, %s, %s, %s)
            """,
            [
                ("first-abcdef12", now, now + timedelta(hours=1), None),
                (
                    "second-abcdef12",
                    now + timedelta(seconds=1),
                    now + timedelta(hours=1),
                    None,
                ),
                ("revoked-abcdef12", now, now + timedelta(hours=1), now),
                ("expired-abcdef12", now, now - timedelta(seconds=1), None),
                ("other-zzzz9999", now, now + timedelta(hours=1), None),
            ],
        )

    assert session_store.session_ids_with_suffix(pool, "abcdef12") == [
        "second-abcdef12",
        "first-abcdef12",
    ]
    assert session_store.session_ids_with_suffix(pool, "zzzz9999") == ["other-zzzz9999"]
    assert session_store.session_ids_with_suffix(pool, "nope") == []
