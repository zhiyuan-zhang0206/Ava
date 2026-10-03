"""Contract tests for the PostgreSQL-backed browser session store."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from gateway.auth import session_store
from gateway.auth.session_store import (
    SessionStore,
    list_sessions,
    minted_session_id,
    touch_session,
)

_MINT = "human-" + "0" * 32
_ADMITTED = frozenset({_MINT})


def _id(name: str) -> str:
    return f"{_MINT}.{name}"


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


@pytest.fixture
def store(pool: ConnectionPool[psycopg.Connection[Any]]) -> SessionStore:
    return SessionStore(pool)


def test_create_validate_revoke_lifecycle(
    pool: ConnectionPool[psycopg.Connection[Any]],
    store: SessionStore,
) -> None:
    store.create(_id("session-one"), 3600, "test-agent", "127.0.0.1")

    assert store.is_valid(_id("session-one"), admitted=_ADMITTED) is True
    assert store.revoke(_id("session-one")) is True
    assert store.is_valid(_id("session-one"), admitted=_ADMITTED) is False
    assert store.revoke(_id("session-one")) is False
    assert store.revoke("missing") is False


def test_revoke_expired_session_returns_false_without_marking_revoked(
    pool: ConnectionPool[psycopg.Connection[Any]],
    store: SessionStore,
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-session",),
        )

    assert store.revoke("expired-session") is False
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
    store: SessionStore,
) -> None:
    checked_at = datetime.now(UTC)
    expires_at = checked_at + timedelta(seconds=5)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, %s)",
            (_id("short-session"), expires_at),
        )

    assert store.is_valid(_id("short-session"), admitted=_ADMITTED, now=checked_at) is True
    assert (
        store.is_valid(
            _id("short-session"),
            admitted=_ADMITTED,
            now=expires_at + timedelta(microseconds=1),
        )
        is False
    )


def test_valid_cache_hit_does_not_borrow_connection(
    pool: ConnectionPool[psycopg.Connection[Any]],
    store: SessionStore,
) -> None:
    checked_at = datetime.now(UTC)
    store.create(_id("cached-session"), 3600, "", "")
    assert store.is_valid(_id("cached-session"), admitted=_ADMITTED, now=checked_at) is True

    class FailingPool:
        def connection(self) -> None:
            raise AssertionError("cache hit borrowed a database connection")

    offline = SessionStore(FailingPool(), cache=store._cache)  # type: ignore[arg-type]
    assert (
        offline.is_valid(
            _id("cached-session"),
            admitted=_ADMITTED,
            now=checked_at + timedelta(seconds=1),
        )
        is True
    )


def test_a_session_whose_mint_is_no_longer_admitted_is_refused_before_any_lookup(
    pool: ConnectionPool[psycopg.Connection[Any]],
    store: SessionStore,
) -> None:
    """The minting credential is checked first: a live row (even a cached one)
    whose mint the caller no longer admits is refused without a database borrow,
    as is an id that carries no mint at all."""
    checked_at = datetime.now(UTC)
    store.create(_id("rotated-away"), 3600, "", "")
    assert store.is_valid(_id("rotated-away"), admitted=_ADMITTED, now=checked_at)

    class FailingPool:
        def connection(self) -> None:
            raise AssertionError("an unadmitted mint borrowed a database connection")

    offline = SessionStore(FailingPool(), cache=store._cache)  # type: ignore[arg-type]
    refused: tuple[tuple[str, frozenset[str]], ...] = (
        (_id("rotated-away"), frozenset({"runner-" + "1" * 32})),
        (_id("rotated-away"), frozenset[str]()),
        ("rotated-away", _ADMITTED),  # no mint
        (f"{_MINT}.", _ADMITTED),
    )
    for session_id, admitted in refused:
        assert (
            offline.is_valid(
                session_id,
                admitted=admitted,
                now=checked_at + timedelta(seconds=1),
            )
            is False
        )


def test_minted_ids_carry_their_mint_and_fresh_randomness(store: SessionStore) -> None:
    first, second = minted_session_id(_MINT), minted_session_id(_MINT)
    assert first != second
    assert session_store.session_mint(first) == _MINT == session_store.session_mint(second)
    assert len(first.partition(".")[2]) >= 43  # 256 random bits, base64url


def test_touch_advances_last_seen_at(
    pool: ConnectionPool[psycopg.Connection[Any]],
    store: SessionStore,
) -> None:
    store.create("touched-session", 3600, "", "")
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
    store: SessionStore,
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-row",),
        )

    store.create("live-row", 3600, "", "")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM web_sessions ORDER BY id")
        assert [row[0] for row in cur.fetchall()] == ["expired-row", "live-row"]


def test_session_cache_evicts_least_recently_used_entry(
    pool: ConnectionPool[psycopg.Connection[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-lived gateway retains only the most recently used session cache entries."""
    store = SessionStore(pool, max_cache_entries=2)
    checked_at = datetime.now(UTC)
    for session_id in (_id("first"), _id("second"), _id("third")):
        store.create(session_id, 3600, "", "")

    assert store.is_valid(_id("first"), admitted=_ADMITTED, now=checked_at) is True
    assert store.is_valid(_id("second"), admitted=_ADMITTED, now=checked_at) is True
    assert (
        store.is_valid(_id("first"), admitted=_ADMITTED, now=checked_at + timedelta(seconds=1))
        is True
    )
    assert store.is_valid(_id("third"), admitted=_ADMITTED, now=checked_at) is True

    assert list(store._cache) == [_id("first"), _id("third")]


def test_session_cache_tolerates_an_entry_evicted_while_touched(
    pool: ConnectionPool[psycopg.Connection[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent cache eviction never turns a valid session into a 500."""

    class _EvictedOnTouch(OrderedDict[str, tuple[datetime, datetime]]):
        def move_to_end(self, key: str, last: bool = True) -> None:
            self.pop(key)
            super().move_to_end(key, last=last)

    checked_at = datetime.now(UTC)
    cache = _EvictedOnTouch(
        {
            _id("racing-session"): (
                checked_at + timedelta(seconds=30),
                checked_at + timedelta(hours=1),
            )
        }
    )
    store = SessionStore(pool, cache=cache)

    assert store.is_valid(_id("racing-session"), admitted=_ADMITTED, now=checked_at) is True


def test_session_cache_tolerates_an_entry_evicted_after_db_lookup(
    pool: ConnectionPool[psycopg.Connection[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An eviction after a database read never turns session validation into a 500."""

    class _EvictedOnTouch(OrderedDict[str, tuple[datetime, datetime]]):
        def move_to_end(self, key: str, last: bool = True) -> None:
            self.pop(key)
            super().move_to_end(key, last=last)

    store = SessionStore(pool, cache=_EvictedOnTouch())
    store.create(_id("racing-db-session"), 3600, "", "")

    assert store.is_valid(_id("racing-db-session"), admitted=_ADMITTED) is True


def test_session_ids_with_suffix_matches_active_rows_only(
    pool: ConnectionPool[psycopg.Connection[Any]],
    store: SessionStore,
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
