"""`shared.pool_release` — release a pool's idle connections, keep it usable.

Both helpers mirror `psycopg_pool`'s own `_shrink_pool` mutation, so the tests
drive them against real pools on the session test database: release must close
what was idle, count it exactly, and leave the pool growing again on the next
borrow — the resume path after a stop window.
"""

from __future__ import annotations

import psycopg
import psycopg_pool

from shared import db as shared_db
from shared.config import settings
from shared.pool_release import release_idle_async, release_idle_sync


def _sync_pool() -> psycopg_pool.ConnectionPool:
    return shared_db.pool(min_size=0, max_size=2)


def test_sync_release_closes_idle_connections_and_the_next_borrow_reconnects() -> None:
    pool = _sync_pool()
    try:
        idle = pool.getconn(timeout=5.0)
        idle.execute("SELECT 1")
        pool.putconn(idle)
        assert pool.get_stats()["pool_available"] == 1

        assert release_idle_sync(pool) == 1

        assert pool.get_stats()["pool_available"] == 0
        assert idle.closed

        resumed = pool.getconn(timeout=5.0)
        assert not resumed.closed
        resumed.execute("SELECT 1").fetchone()
        pool.putconn(resumed)
    finally:
        pool.close()


def test_sync_release_of_an_empty_pool_is_a_no_op() -> None:
    pool = _sync_pool()
    try:
        assert release_idle_sync(pool) == 0
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone() is not None
    finally:
        pool.close()


def test_sync_release_survives_a_backend_killed_under_the_pool() -> None:
    pool = _sync_pool()
    try:
        idle = pool.getconn(timeout=5.0)
        row = idle.execute("SELECT pg_backend_pid()").fetchone()
        assert row is not None
        old_pid = row[0]
        pool.putconn(idle)
        with psycopg.connect(settings.data_plane.db_url) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (old_pid,))

        assert release_idle_sync(pool) == 1

        resumed = pool.getconn(timeout=5.0)
        assert not resumed.closed
        new_row = resumed.execute("SELECT pg_backend_pid()").fetchone()
        assert new_row is not None
        new_pid = new_row[0]
        assert new_pid != old_pid
        pool.putconn(resumed)
    finally:
        pool.close()


async def test_async_release_closes_idle_connections_and_the_next_borrow_reconnects() -> None:
    async with psycopg_pool.AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url,
        min_size=0,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": None},
        open=False,
    ) as pool:
        conn = await pool.getconn(timeout=5.0)
        await conn.execute("SELECT 1")
        await pool.putconn(conn)
        assert pool.get_stats()["pool_available"] == 1

        assert await release_idle_async(pool) == 1

        assert pool.get_stats()["pool_available"] == 0
        assert conn.closed

        resumed = await pool.getconn(timeout=5.0)
        assert not resumed.closed
        await resumed.execute("SELECT 1")
        await pool.putconn(resumed)
