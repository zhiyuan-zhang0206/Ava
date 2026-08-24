"""`services.events_maintenance.retention.apply_retention` — the daemon's
event-aware retention for the unified `events` table.

Each pass drops a month partition once every event kind in it has outlived its
retention, and prunes the expired kinds from partitions that still hold live
audit data. Runs against an isolated throwaway DB (partition DDL is schema,
not rolled back between tests), carrying a fresh partitioned `events` + legacy
`agent_events` pair so the "legacy tables are untouched" contract is pinned
too.

Pins: fully-expired partition dropped whole (with row count); mixed partition
prunes only the expired kinds and keeps audit; fresh partition untouched;
unknown event names block the whole-partition drop but not the pruning of known
expired ones; DEFAULT and non-month partitions are never touched; the pass is
idempotent; the policy is per event name (the daemon derives it from the
registry `shared/events/contract.py`, tests pass it directly); current/future
month partitions (which the daemon creates every pass) are never dropped — even
when empty; the pass works on a non-autocommit connection (the daemon's pool
contract), commits every partition operation independently, and leaves the
connection IDLE with autocommit restored (a borrowed pool connection must not
leak autocommit=True to the next borrower); the real daemon-shaped pass — pool
connection + real `ensure_month_partitions` + real `apply_retention` — drops/
prunes correctly.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from services.events_maintenance import daemon
from services.events_maintenance.partitions import ensure_month_partitions
from services.events_maintenance.retention import (
    _PRUNE_BATCH_SIZE,
    RetentionResult,
    apply_retention,
)
from shared.config import settings
from shared.daemon_health import LoopProgress

# Fixed "now" so partition ages are deterministic: 2026-08-04, with audit 365d /
# telemetry 90d / log 30d as the default policy.
_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_RETENTION = {"audit": 365, "telemetry": 90, "log": 30}

# Same shape as db/schema.sql: partitioned events + agent_events, month
# partitions created at runtime (by the daemon or by these tests directly).
_SCHEMA = """
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
"""


def _throwaway_db() -> Iterator[str]:
    """Create a throwaway DB with the partitioned schema, yield its URL, drop it
    on teardown (same pattern as the partitions tests)."""
    base_url, _name = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_ttl_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_SCHEMA)  # type: ignore[arg-type]  # trusted multi-statement setup
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
def ttl_db_url(db_conn: psycopg.Connection) -> Iterator[str]:
    """URL of a throwaway DB with a fresh partitioned events + agent_events.
    `db_conn` is only a dependency to guarantee the session cluster is up."""
    _ = db_conn
    yield from _throwaway_db()


@pytest.fixture
def ttl_conn(ttl_db_url: str) -> Iterator[psycopg.Connection]:
    """An autocommit connection to the throwaway DB (the tests manage their own
    DDL; apply_retention manages its own transactions)."""
    with psycopg.connect(ttl_db_url) as conn:
        conn.autocommit = True
        yield conn


def _month_partition(conn: psycopg.Connection, month_start: str) -> None:
    """Create the month partition covering [month_start, first of next month)."""
    start = datetime.fromisoformat(month_start)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(start.year, start.month + 1, 1, tzinfo=UTC)
    name = f"events_{start.year:04d}_{start.month:02d}"
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE {} PARTITION OF events FOR VALUES FROM ({}) TO ({})").format(
                sql.Identifier(name), sql.Literal(start), sql.Literal(end)
            )
        )


def _insert(conn: psycopg.Connection, partition: str, ts: str, event: str) -> None:
    """Insert one events row directly into the given partition (bypassing routing);
    `event` is the event_name the retention map is keyed on (category is set to
    the same literal — retention no longer reads it)."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {} (ts, machine, process, category, event_name, level, source) "
                "VALUES (%s, 'test', 'test', %s, %s, 'info', 'system')"
            ).format(sql.Identifier(partition)),
            (ts, event, event),
        )


def _count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _partition_exists(conn: psycopg.Connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_class WHERE relname = %s AND relnamespace = 'public'::regnamespace",
            (name,),
        )
        return cur.fetchone() is not None


def _apply(
    conn: psycopg.Connection, *, retention: Mapping[str, int] = _RETENTION
) -> RetentionResult:
    return apply_retention(conn, now_utc=_NOW, retention_days=retention)


def test_drops_fully_expired_partition(ttl_conn: psycopg.Connection) -> None:
    """A month partition whose every category has outlived its retention (here:
    Jan 2025, ~19 months old, past even the 365d audit pole) is dropped whole,
    with the row count reported."""
    _month_partition(ttl_conn, "2025-01-01T00:00:00+00:00")
    for category in ("audit", "telemetry", "log"):
        _insert(ttl_conn, "events_2025_01", "2025-01-15 10:00+00", category)

    result = _apply(ttl_conn)

    assert len(result.dropped) == 1
    drop = result.dropped[0]
    assert drop.partition == "events_2025_01"
    assert drop.rows == 3
    assert set(drop.events) == {"audit", "telemetry", "log"}
    assert not result.pruned
    assert not _partition_exists(ttl_conn, "events_2025_01")
    assert _count(ttl_conn, "events") == 0


def test_prunes_expired_categories_keeps_audit(ttl_conn: psycopg.Connection) -> None:
    """A mixed partition past telemetry/log retention but inside the audit
    window (Nov 2025, ~8 months) keeps its audit rows and loses only the expired
    telemetry + log rows — the partition itself survives."""
    _month_partition(ttl_conn, "2025-11-01T00:00:00+00:00")
    _insert(ttl_conn, "events_2025_11", "2025-11-15 10:00+00", "audit")
    _insert(ttl_conn, "events_2025_11", "2025-11-15 11:00+00", "telemetry")
    _insert(ttl_conn, "events_2025_11", "2025-11-15 12:00+00", "log")

    result = _apply(ttl_conn)

    assert not result.dropped
    pruned = {(p.event, p.rows) for p in result.pruned}
    assert pruned == {("telemetry", 1), ("log", 1)}
    assert _partition_exists(ttl_conn, "events_2025_11")
    with ttl_conn.cursor() as cur:
        cur.execute("SELECT event_name FROM events ORDER BY ts")
        assert [r[0] for r in cur.fetchall()] == ["audit"]


def test_keeps_fresh_partition(ttl_conn: psycopg.Connection) -> None:
    """A partition inside every retention window (Jul 2026, ~3 days old) is
    untouched — nothing dropped, nothing pruned."""
    _month_partition(ttl_conn, "2026-07-01T00:00:00+00:00")
    for category in ("audit", "telemetry", "log"):
        _insert(ttl_conn, "events_2026_07", "2026-07-20 10:00+00", category)

    result = _apply(ttl_conn)

    assert not result.dropped and not result.pruned
    assert _count(ttl_conn, "events") == 3


def test_partition_boundary_is_exclusive(ttl_conn: psycopg.Connection) -> None:
    """The month grid is [first-of-month, first-of-next): a row at the very end
    of Nov 2025 expires with the rest of November, not a day early."""
    _month_partition(ttl_conn, "2025-11-01T00:00:00+00:00")
    # Nov 30 23:59:59 is inside [2025-11-01, 2025-12-01) — age > 90d, so log is
    # expired; audit still live.
    _insert(ttl_conn, "events_2025_11", "2025-11-30 23:59:59+00", "log")
    _insert(ttl_conn, "events_2025_11", "2025-11-30 23:59:59+00", "audit")

    result = _apply(ttl_conn)

    assert not result.dropped
    assert [(p.event, p.rows) for p in result.pruned] == [("log", 1)]
    with ttl_conn.cursor() as cur:
        cur.execute("SELECT event_name FROM events")
        assert [r[0] for r in cur.fetchall()] == ["audit"]


def test_idempotent(ttl_conn: psycopg.Connection) -> None:
    """A second pass after a successful one is a no-op: the dropped partition is
    gone, the pruned rows stay gone, and nothing new is reported."""
    _month_partition(ttl_conn, "2025-01-01T00:00:00+00:00")
    for category in ("audit", "telemetry", "log"):
        _insert(ttl_conn, "events_2025_01", "2025-01-15 10:00+00", category)
    _month_partition(ttl_conn, "2025-11-01T00:00:00+00:00")
    _insert(ttl_conn, "events_2025_11", "2025-11-15 10:00+00", "audit")
    _insert(ttl_conn, "events_2025_11", "2025-11-15 11:00+00", "log")

    first = _apply(ttl_conn)
    second = _apply(ttl_conn)

    assert len(first.dropped) == 1 and first.pruned
    assert not second.dropped and not second.pruned
    assert _count(ttl_conn, "events") == 1  # only the Nov audit row


def test_respects_configured_retention(ttl_conn: psycopg.Connection) -> None:
    """The per-category policy is configurable: with audit shortened to 30d and
    telemetry lengthened to 1000d, a June partition's audit rows expire (and
    with them the whole partition, since log is also past 30d) while telemetry
    survives."""
    _month_partition(ttl_conn, "2026-06-01T00:00:00+00:00")
    for category in ("audit", "telemetry", "log"):
        _insert(ttl_conn, "events_2026_06", "2026-06-15 10:00+00", category)

    result = _apply(ttl_conn, retention={"audit": 30, "telemetry": 1000, "log": 30})

    # audit (50d old) and log (50d old) both past 30d; telemetry (50d) inside
    # its 1000d window -> partition not dropped, telemetry row kept.
    assert not result.dropped
    assert {(p.event, p.rows) for p in result.pruned} == {("audit", 1), ("log", 1)}
    with ttl_conn.cursor() as cur:
        cur.execute("SELECT event_name FROM events")
        assert [r[0] for r in cur.fetchall()] == ["telemetry"]


def test_unknown_event_blocks_drop_but_prunes_known(
    ttl_conn: psycopg.Connection,
) -> None:
    """An event name outside the retention map is never deleted and blocks the
    whole-partition drop — but the known expired kinds beside it are still
    pruned."""
    _month_partition(ttl_conn, "2025-01-01T00:00:00+00:00")
    for category in ("audit", "telemetry", "log"):
        _insert(ttl_conn, "events_2025_01", "2025-01-15 10:00+00", category)
    _insert(ttl_conn, "events_2025_01", "2025-01-16 10:00+00", "mystery")

    result = _apply(ttl_conn)

    assert not result.dropped  # mystery blocks the drop
    assert {(p.event, p.rows) for p in result.pruned} == {
        ("audit", 1),
        ("telemetry", 1),
        ("log", 1),
    }
    assert _partition_exists(ttl_conn, "events_2025_01")
    with ttl_conn.cursor() as cur:
        cur.execute("SELECT event_name FROM events")
        assert [r[0] for r in cur.fetchall()] == ["mystery"]


def test_skips_default_and_non_month_partitions(ttl_conn: psycopg.Connection) -> None:
    """The DEFAULT catch-all and any non-YYYY_MM partition under events are not
    droppable units — their rows are left alone (a real DEFAULT row also never
    reaches here in steady state: partition rolling carves it into a month)."""
    with ttl_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE events_legacy PARTITION OF events "
            "FOR VALUES FROM (MINVALUE) TO ('2025-01-01 00:00:00+00')"
        )
    _insert(ttl_conn, "events_default", "2025-01-15 10:00+00", "log")
    _insert(ttl_conn, "events_legacy", "2024-06-15 10:00+00", "log")

    result = _apply(ttl_conn)

    assert not result.dropped and not result.pruned
    assert _count(ttl_conn, "events_default") == 1
    assert _count(ttl_conn, "events_legacy") == 1


def test_drops_empty_expired_partition(ttl_conn: psycopg.Connection) -> None:
    """An empty month partition strictly in the past is dropped too (nothing to
    keep; the empty shell would otherwise live forever)."""
    _month_partition(ttl_conn, "2025-01-01T00:00:00+00:00")

    result = _apply(ttl_conn)

    assert len(result.dropped) == 1
    assert result.dropped[0].rows == 0 and result.dropped[0].events == ()
    assert not _partition_exists(ttl_conn, "events_2025_01")


def test_does_not_touch_agent_events(ttl_conn: psycopg.Connection) -> None:
    """The legacy agent_events table is out of scope for this slice: even with
    old rows present, nothing is dropped or pruned there."""
    with ttl_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE agent_events_2025_01 PARTITION OF agent_events "
            "FOR VALUES FROM ('2025-01-01 00:00:00+00') TO ('2025-02-01 00:00:00+00')"
        )
        cur.execute(
            "INSERT INTO agent_events_2025_01 (ts, level, event) "
            "VALUES ('2025-01-15 10:00+00', 'INFO', 'x')"
        )

    result = _apply(ttl_conn)

    assert not result.dropped and not result.pruned
    assert _count(ttl_conn, "agent_events_2025_01") == 1


def test_retention_policy_from_registry_with_settings_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's policy is derived from the event registry (the single
    source of truth): every registered event gets `retention_days()` (spec
    override, else its category floor), and the settings knobs act as optional
    per-category overrides on top — None (the default) means the registry
    value, not a hard-coded copy."""
    from shared.events.contract import RETENTION_BY_CATEGORY

    # No overrides set: registry defaults rule.
    monkeypatch.setattr(settings.daemon, "events_retention_audit_days", None)
    monkeypatch.setattr(settings.daemon, "events_retention_telemetry_days", None)
    monkeypatch.setattr(settings.daemon, "events_retention_log_days", None)
    policy = daemon._retention_policy()
    assert policy["spawn"] == RETENTION_BY_CATEGORY["audit"]
    assert policy["llm_usage"] == RETENTION_BY_CATEGORY["telemetry"]
    assert policy["log"] == RETENTION_BY_CATEGORY["log"]

    # Explicit overrides win over the registry floors.
    monkeypatch.setattr(settings.daemon, "events_retention_audit_days", 700)
    monkeypatch.setattr(settings.daemon, "events_retention_telemetry_days", 120)
    monkeypatch.setattr(settings.daemon, "events_retention_log_days", 45)
    policy = daemon._retention_policy()
    assert policy["spawn"] == 700
    assert policy["llm_usage"] == 120
    assert policy["log"] == 45


def test_daemon_maintenance_includes_retention(
    ttl_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The maintenance pass runs the retention slice after partition creation
    (and before the rollup) on its own connection, and logs the summary."""
    # The retention slice is one of the events-archive slices `_run_maintenance`
    # gates on the (default-off) events flag — pin it on.
    monkeypatch.setattr(daemon.settings.daemon, "events_maintenance_enabled", True)
    pool: ConnectionPool = ConnectionPool(ttl_db_url, min_size=1, max_size=2, open=True)
    calls: list[tuple[psycopg.Connection, dict[str, object]]] = []

    def _fake_retention(conn: psycopg.Connection, **kwargs: object) -> RetentionResult:
        calls.append((conn, kwargs))
        return RetentionResult()

    def _fake_rollup(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(start_day=None)

    def _fake_replay(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(days_replayed=[], days_failed=[], metrics_rows=0, tokens_rows=0)

    def _fake_table_retention(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(deleted=())

    monkeypatch.setattr(daemon, "apply_retention", _fake_retention)
    monkeypatch.setattr(daemon, "compute_rollup", _fake_rollup)
    monkeypatch.setattr(daemon, "replay_gap_days", _fake_replay)
    monkeypatch.setattr(daemon, "apply_table_retention", _fake_table_retention)
    try:
        with pool:
            with pool.connection() as conn:
                ensure_month_partitions(conn, now_utc=_NOW)
            daemon._run_maintenance(pool, LoopProgress("dispatch", timeout_s=60.0))
    finally:
        pool.close()

    assert len(calls) == 1
    _conn, kwargs = calls[0]
    assert kwargs["now_utc"] is not None
    # The pass hands the retention slice the registry-derived per-event policy
    # (every registered event, floors from the contract — e.g. telemetry 90d).
    assert kwargs["retention_days"] == daemon._retention_policy()


def test_does_not_drop_current_or_next_month_partitions(ttl_conn: psycopg.Connection) -> None:
    """A8 regression: retention must never touch the current or next UTC month —
    the daemon ensures both partitions every pass, and a not-yet-started month is
    always empty. Dropping it would delete the partition the daemon just created,
    every hourly pass, each time taking ACCESS EXCLUSIVE on `events`. Only months
    strictly in the past may be dropped, empty or not."""
    _month_partition(ttl_conn, "2026-08-01T00:00:00+00:00")  # current month at _NOW
    _month_partition(ttl_conn, "2026-09-01T00:00:00+00:00")  # next month — empty

    result = _apply(ttl_conn)

    assert not result.dropped and not result.pruned
    assert _partition_exists(ttl_conn, "events_2026_08")
    assert _partition_exists(ttl_conn, "events_2026_09")


def test_apply_retention_on_non_autocommit_connection(ttl_db_url: str) -> None:
    """A9 regression: on a connection with autocommit=False (the daemon's pool
    contract), the pass must not run as one implicit transaction. The entry point
    switches the connection to autocommit, so every partition operation commits
    independently, the first DROP's ACCESS EXCLUSIVE lock is never held across the
    pass, and the connection is left IDLE with autocommit RESTORED to False (a
    borrowed pool connection must not leak autocommit=True to the next borrower —
    psycopg_pool does not reset it on return; Fable audit P1)."""
    with psycopg.connect(ttl_db_url) as conn:
        assert conn.autocommit is False
        _month_partition(conn, "2025-01-01T00:00:00+00:00")
        for event in ("audit", "telemetry", "log"):
            _insert(conn, "events_2025_01", "2025-01-15 10:00+00", event)
        conn.commit()  # end the seeding transaction before the pass starts

        result = _apply(conn)

        assert len(result.dropped) == 1
        assert conn.autocommit is False
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert not _partition_exists(conn, "events_2025_01")


def test_real_maintenance_pass_on_pool_connection(ttl_db_url: str) -> None:
    """A11: the daemon's real pass shape — a non-autocommit pool connection, real
    `ensure_month_partitions`, then real `apply_retention` in the same run (the
    wiring test monkeypatches apply_retention away and `ttl_conn` hardcodes
    autocommit, so this is the only test exercising the actual combined path).
    Pins A8 (the just-created current/next month partitions survive retention)
    and A9 (the pool connection is left IDLE — not inside one implicit
    transaction holding the parent lock)."""
    pool: ConnectionPool = ConnectionPool(ttl_db_url, min_size=1, max_size=2, open=True)
    try:
        with pool:
            # Seed post-migration history: one fully-expired month, one mixed month.
            with pool.connection() as conn:
                _month_partition(conn, "2025-01-01T00:00:00+00:00")
                for event in ("audit", "telemetry", "log"):
                    _insert(conn, "events_2025_01", "2025-01-15 10:00+00", event)
                _month_partition(conn, "2025-11-01T00:00:00+00:00")
                _insert(conn, "events_2025_11", "2025-11-15 10:00+00", "audit")
                _insert(conn, "events_2025_11", "2025-11-15 11:00+00", "log")
            # The real pass: ensure current + next month partitions, then retention.
            with pool.connection() as conn:
                assert conn.autocommit is False
                created = ensure_month_partitions(conn, now_utc=_NOW)
                assert {"events_2026_08", "events_2026_09"} <= set(created)
            with pool.connection() as conn:
                result = apply_retention(conn, now_utc=_NOW, retention_days=_RETENTION)
                assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

            # Only the fully-expired past month was dropped; the current + next
            # month partitions the daemon just created survive (A8).
            assert [d.partition for d in result.dropped] == ["events_2025_01"]
            assert {(p.partition, p.event, p.rows) for p in result.pruned} == {
                ("events_2025_11", "log", 1),
            }
            with pool.connection() as conn:
                assert _partition_exists(conn, "events_2026_08")
                assert _partition_exists(conn, "events_2026_09")
                with conn.cursor() as cur:
                    cur.execute("SELECT event_name FROM events")
                    assert [r[0] for r in cur.fetchall()] == ["audit"]
    finally:
        pool.close()


def test_prune_batches_large_partition(ttl_conn: psycopg.Connection) -> None:
    """A10: pruning more rows than one batch loops across transactions (each
    batch commits independently, bounded by `_PRUNE_BATCH_SIZE`) and still
    reports the full deleted count; the partition survives."""
    _month_partition(ttl_conn, "2025-11-01T00:00:00+00:00")
    total = _PRUNE_BATCH_SIZE * 2 + 123  # spans three batches
    # One audit row keeps the partition alive (audit is inside its 365d window),
    # so the log rows below must be pruned in batches rather than dropped whole.
    _insert(ttl_conn, "events_2025_11", "2025-11-15 09:00:00+00", "audit")
    with ttl_conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {} (ts, machine, process, category, event_name, level, source) "
                "SELECT '2025-11-15 10:00:00+00'::timestamptz + (g || ' seconds')::interval, "
                "'test', 'test', 'log', 'log', 'info', 'system' "
                "FROM generate_series(0, {}) g"
            ).format(sql.Identifier("events_2025_11"), sql.Literal(total - 1))
        )

    result = _apply(ttl_conn)

    assert not result.dropped
    assert [(p.event, p.rows) for p in result.pruned] == [("log", total)]
    assert _partition_exists(ttl_conn, "events_2025_11")
    assert _count(ttl_conn, "events") == 1  # the audit row survives
