"""`services.events_maintenance.blob_vacuum` — incremental physical reclamation.

Pins (Task #1130): a plain VACUUM (never FULL — FULL's ACCESS EXCLUSIVE lock
would stall agents, ruled out by the user); only inside the measured
agent-lowest window (05:00-08:00 America/Los_Angeles); each run logs the
physical size + dead-tuple count so the reclamation trend is observable.

The window boundary is the load-bearing part (the daemon calls this hourly, so
a wrong window means vacuuming at peak hours); the VACUUM itself is exercised
against the throwaway DB for the ran/not-ran contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.events_maintenance.blob_vacuum import (
    in_low_traffic_window,
    run_blob_vacuum,
    vacuum_checkpoint_tables,
)

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
"""


@pytest.fixture
def pool(db_conn: psycopg.Connection) -> Iterator[ConnectionPool[Any]]:
    """A pool to a throwaway DB carrying the checkpoint tables."""
    _ = db_conn
    from tests.services.test_events_maintenance_ttl import _throwaway_db

    gen = cast(Any, _throwaway_db())
    url = next(gen)  # keep the generator alive — GC would run its finally (DROP DATABASE)
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_SCHEMA)  # type: ignore[argtype]  # trusted multi-statement setup
        # autocommit: VACUUM cannot run inside a transaction block, and the
        # daemon's maintenance pool is autocommit by construction.
        with ConnectionPool(
            url, min_size=1, max_size=2, open=True, kwargs={"autocommit": True}
        ) as p:
            yield p
    finally:
        gen.close()


def _pdt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A tz-aware datetime at the given PDT wall-clock time."""
    return datetime(
        2026,
        8,
        10,
        hour,
        minute,
        second,
        tzinfo=__import__("zoneinfo").ZoneInfo("America/Los_Angeles"),
    )


def test_window_boundaries() -> None:
    """05:00-08:00 PDT inclusive start, exclusive end."""
    assert in_low_traffic_window(_pdt(5, 0))
    assert in_low_traffic_window(_pdt(7, 59, 59))
    assert not in_low_traffic_window(_pdt(4, 59, 59))
    assert not in_low_traffic_window(_pdt(8, 0))
    assert not in_low_traffic_window(_pdt(11, 0))  # measured peak
    assert not in_low_traffic_window(_pdt(23, 0))


def test_run_skips_outside_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the window the daemon entry point no-ops without dialing.

    The clock is frozen to 03:00 America/Los_Angeles (10:00 UTC): the suite
    runs at all hours (CI hits the 05:00-08:00 PDT vacuum window daily), and
    the real clock made this test flaky — inside the window it dialed the DB
    and vacuumed, failing the ran=False assertion (observed 2026-08-11
    merge-queue CI at 12:13Z). The gate itself is locked by
    test_window_boundaries.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 — the frozen instant carries its own tz  # pyright: ignore[reportMissingParameterType]
            return datetime(2026, 8, 10, 10, 0, tzinfo=UTC)  # 03:00 PDT — outside window

    monkeypatch.setattr("services.events_maintenance.blob_vacuum.datetime", _Frozen)
    result = run_blob_vacuum()
    assert result.ran is False
    assert result.total_bytes == 0


def test_run_vacuums_in_window(pool: ConnectionPool[Any]) -> None:
    """A pass executes VACUUM (ANALYZE) on all three tables and reports
    physical size + dead tuples without error (VACUUM runs on the caller's
    autocommit connection)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "checkpoint, metadata) VALUES ('t', '', 'c1', '{}', '{}')"
            )
        # delete the row so a dead tuple exists for the stats to observe
        with conn.cursor() as cur:
            cur.execute("DELETE FROM checkpoints WHERE thread_id = 't'")
        result = vacuum_checkpoint_tables(conn)
    assert result.ran is True
    assert result.total_bytes > 0
    assert result.dead_tuples >= 0


def test_run_skips_missing_tables_fresh_cluster(
    monkeypatch: pytest.MonkeyPatch, pool: ConnectionPool[Any]
) -> None:
    """A fresh cluster without checkpoint tables must read as a no-op, not a
    ProgrammingError crash — the maintenance daemon exits on ProgrammingError,
    so an unguarded UndefinedTable would crash-loop the daily window
    (adversarial review of #2226)."""

    def _fake_connect(*_a, **_k):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        return pool.connection()  # PoolConnection — usable as a `with` target

    import shared.db

    monkeypatch.setattr(shared.db, "connect", _fake_connect)  # pyright: ignore[reportUnknownArgumentType]
    # Drop the tables the fixture created, simulating a greenfield cluster.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE checkpoint_blobs, checkpoints, checkpoint_writes")

    result = run_blob_vacuum(force=True)
    assert result.ran is False
    assert result.total_bytes == 0


def test_run_ok_with_same_named_table_in_other_schema(
    pool: ConnectionPool[Any],
) -> None:
    """A same-named checkpoint table in another schema must not break the
    physical-state query: the scalar subquery over pg_stat_user_tables used to
    match every schema, and the forensics `recovery` schema carries its own
    checkpoint_blobs — one row each in public + recovery made the subquery
    return two rows, a CardinalityViolation the daemon misreads as schema
    drift and exits on (crash loop 2026-08-12). Regression for the
    schemaname='public' pin."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA recovery")
            cur.execute("CREATE TABLE recovery.checkpoint_blobs (LIKE checkpoint_blobs)")
        result = vacuum_checkpoint_tables(conn)
    assert result.ran is True
    assert result.total_bytes > 0
