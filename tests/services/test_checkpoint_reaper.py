"""Checkpoint retention through the events-maintenance daemon's fast loop.

Every thread with more than three checkpoints is pruned to its newest three,
independent of agent status or liveness. The tests exercise real Postgres
tables and pin rotation, the productive-thread cap, eligibility re-check,
compaction-boundary exemption, and the shared trim's in-flight write guard.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

import services.events_maintenance.checkpoint_reaper as reaper
from services.events_maintenance.checkpoint_reaper import ReapCounts
from shared.checkpoint_cleanup import TrimCounts, trim_checkpoints_sync
from shared.config import settings

_SCHEMA = """
CREATE TABLE checkpoints (
    thread_id            TEXT NOT NULL,
    checkpoint_ns        TEXT NOT NULL DEFAULT '',
    checkpoint_id        TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type                 TEXT,
    checkpoint           JSONB NOT NULL,
    metadata             JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE checkpoint_blobs (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    type          TEXT NOT NULL,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE TABLE checkpoint_writes (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    type          TEXT,
    blob          BYTEA NOT NULL,
    task_path     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
CREATE TABLE agents_meta (
    id             BIGINT PRIMARY KEY,
    status         TEXT NOT NULL,
    last_active_at TIMESTAMPTZ DEFAULT now()
);
"""


def _throwaway_db(db_conn: psycopg.Connection) -> Iterator[str]:
    """Create a throwaway DB, yield its URL, and drop it on teardown."""
    _ = db_conn  # dependency: the session cluster is up
    base_url, _name = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_reaper_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def pool(db_conn: psycopg.Connection) -> Iterator[ConnectionPool[Any]]:
    """A pool to a throwaway DB carrying checkpoint and agent metadata tables."""
    gen = cast(Any, _throwaway_db(db_conn))
    url = next(gen)  # keep the generator alive until the fixture teardown
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_SCHEMA)  # type: ignore[arg-type]  # trusted test schema
        with ConnectionPool(url, min_size=1, max_size=2, open=True) as db_pool:
            yield db_pool
    finally:
        gen.close()


def _agent(
    pool: ConnectionPool[Any],
    agent_id: int,
    status: str,
    *,
    last_active_at: datetime | None = None,
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        if last_active_at is None:
            cur.execute(
                "INSERT INTO agents_meta (id, status) VALUES (%s, %s)",
                (agent_id, status),
            )
        else:
            cur.execute(
                "INSERT INTO agents_meta (id, status, last_active_at) VALUES (%s, %s, %s)",
                (agent_id, status, last_active_at),
            )


def _version(counter: int) -> str:
    return f"{counter:032}.test"


def _thread(
    pool: ConnectionPool[Any],
    thread_id: str,
    checkpoints: int,
    *,
    checkpoint_ns: str = "",
    messages_version: str | None = None,
    boundary_at: int | None = None,
) -> list[str]:
    """Insert checkpoints with writes and one shared, committed messages blob."""
    version = messages_version or _version(1)
    checkpoint_ids: list[str] = []
    with pool.connection() as conn, conn.cursor() as cur:
        for index in range(checkpoints):
            checkpoint_id = f"ckpt-{thread_id}-{index:03d}"
            checkpoint_ids.append(checkpoint_id)
            metadata = '{"compact_boundary": true}' if index == boundary_at else "{}"
            cur.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "checkpoint, metadata) VALUES (%s, %s, %s, %s, %s)",
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    f'{{"channel_versions": {{"messages": "{version}"}}}}',
                    metadata,
                ),
            )
            cur.execute(
                "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, "
                "task_id, idx, channel, blob) VALUES (%s, %s, %s, 't', 0, 'messages', %s)",
                (thread_id, checkpoint_ns, checkpoint_id, f"write-{thread_id}-{index}".encode()),
            )
        cur.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, "
            "type, blob) VALUES (%s, %s, 'messages', %s, 'json', %s)",
            (thread_id, checkpoint_ns, version, f"blob-{thread_id}".encode()),
        )
    return checkpoint_ids


def _count(pool: ConnectionPool[Any], table: str, thread_id: str | None = None) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        if thread_id is None:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — test constant
        else:
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id = %s",  # noqa: S608
                (thread_id,),
            )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _checkpoint_ids(pool: ConnectionPool[Any], thread_id: str) -> set[str]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s", (thread_id,))
        return {row[0] for row in cur.fetchall()}


def test_rotation_window_advances_per_minute_and_is_idempotent() -> None:
    candidates = [str(index) for index in range(130)]
    windows = [
        reaper._rotate_candidates(candidates, max_threads=64, now_seconds=now)
        for now in (0.0, 60.0, 120.0)
    ]

    assert windows[0] == reaper._rotate_candidates(candidates, max_threads=64, now_seconds=0.0)
    for window in windows:
        assert len(window) == len(candidates) and set(window) == set(candidates)
    offsets = [candidates.index(window[0]) for window in windows]
    assert [(right - left) % 130 for left, right in pairwise(offsets)] == [64, 64]
    assert set().union(*(set(window[:64]) for window in windows)) == set(candidates)


def test_prunes_all_thread_liveness_classes_to_keep_three(
    pool: ConnectionPool[Any],
) -> None:
    _agent(pool, 101, "terminated")
    _agent(pool, 102, "idling", last_active_at=datetime.now(UTC) - timedelta(hours=48))
    _agent(pool, 103, "running")
    for thread_id in ("101", "102", "103", "orphan"):
        _thread(pool, thread_id, 5)

    counts = reaper.prune_threads(pool)

    assert counts == ReapCounts(agents=4, checkpoints=8, writes=8, blobs=0)
    for thread_id in ("101", "102", "103", "orphan"):
        assert _count(pool, "checkpoints", thread_id) == 3
        assert _count(pool, "checkpoint_writes", thread_id) == 3
        assert _count(pool, "checkpoint_blobs", thread_id) == 1


def test_prune_does_not_require_agents_meta_table(pool: ConnectionPool[Any]) -> None:
    _thread(pool, "not-an-agent", 4)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE agents_meta")

    assert reaper.prune_threads(pool) == ReapCounts(agents=1, checkpoints=1, writes=1, blobs=0)


def test_prune_keeps_exactly_newest_three(pool: ConnectionPool[Any]) -> None:
    checkpoint_ids = _thread(pool, "251", 6)

    counts = reaper.prune_threads(pool)

    assert counts == ReapCounts(agents=1, checkpoints=3, writes=3, blobs=0)
    assert _checkpoint_ids(pool, "251") == set(checkpoint_ids[-3:])


def test_prune_skips_threads_at_or_below_keep(pool: ConnectionPool[Any]) -> None:
    _thread(pool, "261", 3)
    _thread(pool, "262", 2)

    assert reaper.prune_threads(pool) == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints") == 5


def test_compaction_boundary_is_exempt_from_prune(pool: ConnectionPool[Any]) -> None:
    """The only out-of-window row is a boundary — the thread is not trimmed
    and does not consume a productive slot."""
    checkpoint_ids = _thread(pool, "271", 4, boundary_at=0)

    assert reaper._thread_counts(pool)["271"] == 4
    counts = reaper.prune_threads(pool)

    assert counts == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _checkpoint_ids(pool, "271") == set(checkpoint_ids)


def test_eligibility_recheck_skips_thread_no_longer_above_keep(
    pool: ConnectionPool[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _thread(pool, "301", 5)

    def no_longer_above_keep(_pool: ConnectionPool, _thread_id: str) -> bool:
        return False

    monkeypatch.setattr(reaper, "_still_above_keep", no_longer_above_keep)

    counts = reaper.prune_threads(pool)

    assert counts == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints", "301") == 5


def test_in_flight_guard_skip_is_not_productive(pool: ConnectionPool[Any]) -> None:
    _thread(pool, "guarded", 4)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM checkpoint_blobs WHERE thread_id = 'guarded' AND channel = 'messages'"
        )

    counts = reaper.prune_threads(pool)

    assert counts == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints", "guarded") == 4


def test_guard_skips_do_not_consume_productive_thread_cap(
    pool: ConnectionPool[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(64):
        thread_id = f"blocked-{index:02d}"
        _thread(pool, thread_id, 4)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
    _thread(pool, "productive", 4)
    monkeypatch.setattr(reaper.time, "time", lambda: 0.0)

    counts = reaper.prune_threads(pool)

    assert counts == ReapCounts(agents=1, checkpoints=1, writes=1, blobs=0)
    assert _count(pool, "checkpoints", "productive") == 3


def test_pass_is_idempotent(pool: ConnectionPool[Any]) -> None:
    _thread(pool, "401", 5)

    first = reaper.prune_threads(pool)
    second = reaper.prune_threads(pool)

    assert first == ReapCounts(agents=1, checkpoints=2, writes=2, blobs=0)
    assert second == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints", "401") == 3


def test_sync_trim_matches_counts(pool: ConnectionPool[Any]) -> None:
    _thread(pool, "501", 7)

    counts = trim_checkpoints_sync(pool, "501", keep=3)

    assert counts == TrimCounts(checkpoints=4, writes=4, blobs=0)
    assert _count(pool, "checkpoints", "501") == 3
    assert _count(pool, "checkpoint_writes", "501") == 3
    assert _count(pool, "checkpoint_blobs", "501") == 1


def test_missing_checkpoints_table_is_a_noop(pool: ConnectionPool[Any]) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE checkpoints")

    assert reaper._thread_counts(pool) == {}
    assert reaper.prune_threads(pool) == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)


def test_sync_trim_batches_large_thread(pool: ConnectionPool[Any]) -> None:
    _thread(pool, "large", 120)

    counts = trim_checkpoints_sync(pool, "large", keep=3, batch=50)

    assert counts == TrimCounts(checkpoints=117, writes=117, blobs=0)
    assert _count(pool, "checkpoints", "large") == 3
    assert _count(pool, "checkpoint_writes", "large") == 3
    assert _count(pool, "checkpoint_blobs", "large") == 1


def test_prune_caps_productive_threads_per_pass(pool: ConnectionPool[Any]) -> None:
    for index in range(70):
        _thread(pool, f"thread-{index:02d}", 4)

    first = reaper.prune_threads(pool)
    second = reaper.prune_threads(pool)

    assert first.agents == 64
    assert first.checkpoints == 64
    assert second.agents == 6
    assert second.checkpoints == 6
    assert _count(pool, "checkpoints") == 70 * 3
