"""`services.events_maintenance.partitions.ensure_month_partitions` — the daemon's
monthly partition rolling.

Each pass ensures the current + next UTC-month partitions of the unified
`events` stream exist. The frozen legacy `agent_events` mirror (zero writes
since the migration window closed) is deliberately NOT maintained — the dead
table no longer pays a per-hour partition tax. These run against an isolated
throwaway DB carrying a fresh partitioned pair (parent + DEFAULT, the
db/schema.sql shape) so partition DDL — which is schema, not rolled back
between tests — cannot leak into the shared session DB.

Pins: current+next are created on fresh tables; the call is idempotent; a month
already covered by a wide partition is a no-op, not an overlap error; a month
whose rows are already sitting in DEFAULT is carved out (rows moved into the
new partition, DEFAULT emptied); and agent_events receives no partitions at all.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from services.events_maintenance import daemon
from services.events_maintenance.partitions import ensure_month_partitions
from shared.config import settings
from shared.daemon_health import LoopProgress

# Fixed "now" so current month = 2026-07, next month = 2026-08 deterministically.
_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

# The one maintained event table; the schema mirrors db/schema.sql (parent +
# DEFAULT, month partitions created at runtime). agent_events exists in the
# schema but is frozen — no partitions are ever created for it.
_EVENT_PARENTS = ("events",)

_PARTITIONED_SCHEMA = """
CREATE TABLE agent_events (
    id        BIGSERIAL,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_id  BIGINT,
    level     TEXT NOT NULL,
    event     TEXT NOT NULL,
    payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
CREATE TABLE agent_events_default PARTITION OF agent_events DEFAULT;
CREATE TABLE events (
    id               BIGSERIAL,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id         TEXT,
    span_id          TEXT,
    agent_id         BIGINT,
    machine          TEXT NOT NULL,
    process          TEXT NOT NULL,
    category         TEXT NOT NULL,
    event_name       TEXT NOT NULL,
    level            TEXT NOT NULL,
    source           TEXT NOT NULL,
    target_agent_id  BIGINT,
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
CREATE TABLE events_default PARTITION OF events DEFAULT;
"""


def _throwaway_partitioned_db() -> Iterator[str]:
    """Create a throwaway DB with a fresh partitioned agent_events + events, yield
    its URL, drop it on teardown."""
    base_url, _name = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_partmod_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_PARTITIONED_SCHEMA)  # type: ignore[arg-type]  # trusted multi-statement setup
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
def part_db_url(db_conn: psycopg.Connection) -> Iterator[str]:
    """URL of a throwaway DB with a fresh partitioned pair (agent_events +
    events, the db/schema.sql shape). `db_conn` is only a dependency to
    guarantee the session cluster is up."""
    _ = db_conn
    yield from _throwaway_partitioned_db()


@pytest.fixture
def part_conn(part_db_url: str) -> Iterator[psycopg.Connection]:
    """An autocommit connection to the throwaway partitioned DB
    (ensure_month_partitions manages its own transactions; asserts want to see
    committed DDL)."""
    with psycopg.connect(part_db_url) as conn:
        conn.autocommit = True
        yield conn


def _scalar(cur: psycopg.Cursor) -> Any:
    """First column of the single result row (asserted present — these queries
    always return one row)."""
    row = cur.fetchone()
    assert row is not None
    return row[0]


def _partitions(conn: psycopg.Connection, parent: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = %s ORDER BY c.relname",
            (parent,),
        )
        return [r[0] for r in cur.fetchall()]


def _insert(conn: psycopg.Connection, parent: str, ts: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, machine, process, category, event_name, level, source) "
            "VALUES (%s, 'test', 'test', 'log', 'x', 'info', 'system')",
            (ts,),
        )


def _partition_of(conn: psycopg.Connection, parent: str, ts: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT tableoid::regclass::text FROM {} WHERE ts = %s").format(
                sql.Identifier(parent)
            ),
            (ts,),
        )
        return _scalar(cur)


def test_creates_current_and_next(part_conn: psycopg.Connection) -> None:
    """On fresh partitioned tables (DEFAULT only), the pass creates the current
    and next month partitions for the events table — and NOTHING for the
    frozen agent_events mirror (dead table, no partition maintenance)."""
    created = ensure_month_partitions(part_conn, now_utc=_NOW)
    assert created == ["events_2026_07", "events_2026_08"]
    for parent in _EVENT_PARENTS:
        assert _partitions(part_conn, parent) == [
            f"{parent}_2026_07",
            f"{parent}_2026_08",
            f"{parent}_default",
        ]
    assert _partitions(part_conn, "agent_events") == ["agent_events_default"]


def test_idempotent(part_conn: psycopg.Connection) -> None:
    """A second pass at the same time creates nothing (both months already exist)."""
    ensure_month_partitions(part_conn, now_utc=_NOW)
    assert ensure_month_partitions(part_conn, now_utc=_NOW) == []


def test_rolls_forward_next_month(part_conn: psycopg.Connection) -> None:
    """A month later, only the new next month is created (current already exists)."""
    ensure_month_partitions(part_conn, now_utc=_NOW)
    created = ensure_month_partitions(part_conn, now_utc=datetime(2026, 8, 10, 0, 0, tzinfo=UTC))
    assert created == ["events_2026_09"]  # Aug exists; Sep is new


def test_year_boundary(part_conn: psycopg.Connection) -> None:
    """December rolls the next partition into the following January."""
    created = ensure_month_partitions(part_conn, now_utc=datetime(2026, 12, 15, 0, 0, tzinfo=UTC))
    assert created == ["events_2026_12", "events_2027_01"]


def test_month_covered_by_wide_partition_is_noop(part_conn: psycopg.Connection) -> None:
    """A current month already inside a wide partition (the shape the legacy
    conversion left behind) is not re-created — the overlap is treated as
    covered, not raised. The next month is still created."""
    with part_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE events_legacy PARTITION OF events "
            "FOR VALUES FROM (MINVALUE) TO ('2026-08-01 00:00:00+00')"
        )
    created = ensure_month_partitions(part_conn, now_utc=_NOW)  # July is inside legacy
    assert created == ["events_2026_08"]
    assert "events_2026_07" not in _partitions(part_conn, "events")
    assert _partitions(part_conn, "events") == [
        "events_2026_08",
        "events_default",
        "events_legacy",
    ]


def test_carves_rows_out_of_default(part_conn: psycopg.Connection) -> None:
    """A current-month row already in DEFAULT (a write that beat the first pass) is
    carved into the newly created month partition; DEFAULT is left empty."""
    for parent in _EVENT_PARENTS:
        _insert(
            part_conn, parent, "2026-07-15 10:00+00"
        )  # lands in DEFAULT (no July partition yet)
        assert _partition_of(part_conn, parent, "2026-07-15 10:00+00") == f"{parent}_default"

    created = ensure_month_partitions(part_conn, now_utc=_NOW)
    for parent in _EVENT_PARENTS:
        assert f"{parent}_2026_07" in created
        # The row moved out of DEFAULT into the July partition; nothing lost.
        assert _partition_of(part_conn, parent, "2026-07-15 10:00+00") == f"{parent}_2026_07"
        with part_conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(f"{parent}_default"))
            )
            assert _scalar(cur) == 0
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(parent)))
            assert _scalar(cur) == 1


def test_carves_stranded_older_month_from_default(part_conn: psycopg.Connection) -> None:
    """A month older than current sitting in DEFAULT (the daemon was down across a
    boundary, so an intermediate month's rows landed in the catch-all) is carved out
    too — not just current/next. DEFAULT drains back to empty and the old month
    becomes its own droppable partition."""
    _insert(part_conn, "events", "2026-05-15 10:00+00")  # May, well before now=July -> DEFAULT
    for parent in _EVENT_PARENTS:
        assert _partition_of(part_conn, parent, "2026-05-15 10:00+00") == f"{parent}_default"

    created = ensure_month_partitions(part_conn, now_utc=_NOW)
    for parent in _EVENT_PARENTS:
        assert f"{parent}_2026_05" in created  # the stranded older month is carved
        assert _partition_of(part_conn, parent, "2026-05-15 10:00+00") == f"{parent}_2026_05"
        with part_conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(f"{parent}_default"))
            )
            assert _scalar(cur) == 0  # DEFAULT drained


def test_writes_route_to_created_partition(part_conn: psycopg.Connection) -> None:
    """After the pass, a current-month write routes to the month partition, not
    DEFAULT — for both event tables."""
    ensure_month_partitions(part_conn, now_utc=_NOW)
    for parent in _EVENT_PARENTS:
        _insert(part_conn, parent, "2026-07-20 08:00+00")
        assert _partition_of(part_conn, parent, "2026-07-20 08:00+00") == f"{parent}_2026_07"


def test_daemon_commits_partitions_before_rollup(
    part_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's maintenance pass commits partition creation independently of
    the rollup: even when the rollup raises, the partitions created this pass
    persist (they are not rolled back with the failed rollup). Uses a real
    non-autocommit ConnectionPool — the daemon's actual connection mode — so a
    regression to sharing one connection (DDL held in the rollup's transaction)
    would fail here."""
    # This exercises the events-archive slices, which `_run_maintenance` gates
    # on the (default-off) events flag — pin it on.
    monkeypatch.setattr(daemon.settings.daemon, "events_maintenance_enabled", True)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("rollup blew up")

    monkeypatch.setattr(daemon, "compute_rollup", _boom)

    def _fake_table_retention(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(deleted=())

    monkeypatch.setattr(daemon, "apply_table_retention", _fake_table_retention)

    # Bare-typed like the daemon's own pool (shared.db.pool() -> ConnectionPool), so
    # it matches _run_maintenance's parameter type.
    pool: ConnectionPool = ConnectionPool(part_db_url, min_size=1, max_size=2, open=True)
    with pool:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'agent_events'"
            )
            before_legacy = _scalar(cur)

        with pytest.raises(RuntimeError, match="rollup blew up"):
            daemon._run_maintenance(pool, LoopProgress("dispatch", timeout_s=60.0))

        # Despite the rollup failure, the partition(s) created this pass are committed.
        with pool.connection() as conn, conn.cursor() as cur:
            # The frozen agent_events mirror gains no partitions on the pass.
            cur.execute(
                "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'agent_events'"
            )
            assert _scalar(cur) == before_legacy
            cur.execute(
                "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'events'"
            )
            assert _scalar(cur) > 0  # events partitions committed too
