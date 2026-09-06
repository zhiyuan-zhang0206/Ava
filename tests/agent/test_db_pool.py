"""`LoggingConnectionPool` — a slow or timed-out connection borrow must surface
in the log instead of being a silent multi-second block.

A transiently unresponsive Postgres used to read as a mystery node stall: the
only signal was a gap in the agent log while `getconn` silently waited out its
(default 30s) timeout. These tests pin the logging contract — fast borrow is
quiet, slow borrow WARNs, `PoolTimeout` ERRORs and still propagates — without a
live pg by stubbing the base `getconn` and the clock.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent import db
from agent.db import _SLOW_ACQUIRE_WARN_S, LoggingConnectionPool, claim_inbound_batch


def _pool() -> LoggingConnectionPool[psycopg.AsyncConnection]:
    # open=False → no connection attempt; we never touch a real pg here.
    return LoggingConnectionPool[psycopg.AsyncConnection](
        "postgresql://unused",
        pool_name="ops",
        min_size=0,
        max_size=2,
        open=False,
    )


def _acquire_records(records):
    return [r for r in records if r["extra"].get("event", "").startswith("db_pool_acquire")]  # pyright: ignore[reportUnknownMemberType]


async def test_fast_acquire_is_quiet(loguru_records, monkeypatch: pytest.MonkeyPatch) -> None:
    """A borrow under the slow threshold logs nothing."""
    sentinel = object()

    async def _fast(self, timeout=None):
        return sentinel

    monkeypatch.setattr(AsyncConnectionPool, "getconn", _fast)  # pyright: ignore[reportUnknownArgumentType]
    conn = await _pool().getconn()

    assert conn is sentinel
    assert _acquire_records(loguru_records) == []  # pyright: ignore[reportUnknownArgumentType]


async def test_slow_acquire_warns(loguru_records, monkeypatch: pytest.MonkeyPatch) -> None:
    """A borrow that succeeds but took >= the slow threshold WARNs, named."""

    async def _ok(self, timeout=None):
        return object()

    pool = _pool()  # construct under the real clock, before we stub it
    monkeypatch.setattr(AsyncConnectionPool, "getconn", _ok)  # pyright: ignore[reportUnknownArgumentType]
    # `t0` is the first read; every later read (ours + any loguru-internal one)
    # returns a value past the threshold, so the stub is robust to extra calls.
    reads = {"n": 0}

    def _clock() -> float:
        reads["n"] += 1
        return 0.0 if reads["n"] == 1 else _SLOW_ACQUIRE_WARN_S + 1.0

    monkeypatch.setattr(db.time, "monotonic", _clock)

    await pool.getconn()

    recs = _acquire_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert len(recs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert recs[0]["extra"]["event"] == "db_pool_acquire_slow"
    assert recs[0]["extra"]["name"] == "ops"
    assert recs[0]["level"].name == "WARNING"  # pyright: ignore[reportUnknownMemberType]


async def test_pool_timeout_errors_and_propagates(
    loguru_records, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `PoolTimeout` ERRORs (named) and still raises — behaviour is otherwise
    identical to the base pool, just no longer silent."""

    async def _timeout(self, timeout=None):
        raise PoolTimeout("couldn't get a connection")

    monkeypatch.setattr(AsyncConnectionPool, "getconn", _timeout)  # pyright: ignore[reportUnknownArgumentType]

    pool = _pool()
    monkeypatch.setattr(
        pool,
        "get_stats",
        lambda: {
            "pool_size": 2,
            "pool_available": 0,
            "requests_waiting": 3,
            "connections_errors": 0,
        },
    )

    with pytest.raises(PoolTimeout):
        await pool.getconn()

    recs = _acquire_records(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert len(recs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert recs[0]["extra"]["event"] == "db_pool_acquire_timeout"
    assert recs[0]["extra"]["name"] == "ops"
    assert recs[0]["extra"]["pool_size"] == 2
    assert recs[0]["extra"]["pool_available"] == 0
    assert recs[0]["extra"]["requests_waiting"] == 3
    assert recs[0]["extra"]["connections_errors"] == 0
    assert recs[0]["level"].name == "ERROR"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_pool_timeout_uses_bound_and_releases_borrow() -> None:
    """A timed-out claim round cannot leave a connection checked out."""

    class _Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, _query: str, _params: object = None) -> None:
            raise PoolTimeout("injected claim timeout")

    class _Pool:
        timeout: float | None = None
        held = 0

        @asynccontextmanager
        async def connection(self, timeout: float | None = None):
            self.timeout = timeout
            self.held += 1
            try:
                yield _Connection()
            finally:
                self.held -= 1

    fake = _Pool()
    pool = cast(AsyncConnectionPool[Any], cast(object, fake))

    with pytest.raises(PoolTimeout, match="injected claim timeout"):
        await claim_inbound_batch(pool, 42)

    assert fake.timeout == db._CLAIM_DB_ACQUIRE_TIMEOUT_S
    assert fake.held == 0
