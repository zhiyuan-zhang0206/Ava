"""`services.events_maintenance.reindex` — index-bloat governance for events
partitions (audit M2 / P1-2 ①).

Runs against a throwaway DB with a fresh partitioned `events` table (the
retention tests' pattern), so the per-partition hot indexes exist exactly as
in schema.sql.

Pins: only current + previous UTC-month partitions are candidates; only the
four hot shapes (kind/event_name/category/agent_id/machine + ts) are governed;
the bytes/row decision honours the per-shape threshold (reindexes when past
it, no-ops below); a failed REINDEX is collected, not fatal; the pass works on
an autocommit connection (REINDEX CONCURRENTLY contract).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from services.events_maintenance.reindex import (
    _AGENT_TS_BYTES_PER_ROW,
    _TEXT_TS_BYTES_PER_ROW,
    ReindexResult,
    _threshold_for,
    candidate_indexes,
    reindex_if_bloated,
)
from shared.config import settings

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

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
CREATE INDEX idx_events_agent_ts ON events (agent_id, ts DESC);
CREATE INDEX idx_events_event_name_ts ON events (event_name, ts DESC);
CREATE INDEX idx_events_category_ts ON events (category, ts DESC);
CREATE INDEX idx_events_trace_id ON events (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX idx_events_machine_ts ON events (machine, ts DESC);
CREATE INDEX idx_events_level_ts ON events (ts DESC) WHERE level IN ('warning', 'error');
"""


@pytest.fixture()
def events_conn() -> Iterator[psycopg.Connection]:
    """Autocommit connection to a throwaway DB with a fresh partitioned events
    table (the retention tests' pattern — a separate DB, so the session-level
    `_clean_state` TRUNCATE of the main `events` table never touches it)."""
    base_url, _name = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_reindex_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_SCHEMA)  # type: ignore[arg-type]  # trusted multi-statement setup
            with setup.cursor() as cur:
                cur.execute(
                    "CREATE TABLE events_2026_08 PARTITION OF events FOR VALUES FROM "
                    "('2026-08-01') TO ('2026-09-01')"
                )
                cur.execute(
                    "CREATE TABLE events_2026_07 PARTITION OF events FOR VALUES FROM "
                    "('2026-07-01') TO ('2026-08-01')"
                )
                cur.execute(
                    "CREATE TABLE events_2026_06 PARTITION OF events FOR VALUES FROM "
                    "('2026-06-01') TO ('2026-07-01')"
                )
                # Enough rows that the fixed 1-2 page index overhead does not
                # dominate the bytes/row estimate (tiny tables look "bloated"
                # at 81 B/row by page overhead alone; at prod scale the
                # overhead is negligible).
                for part, base in (
                    ("events_2026_07", "2026-07-15"),
                    ("events_2026_08", "2026-08-15"),
                ):
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {} (ts, machine, process, category, event_name, level, source) "
                            "SELECT {}::timestamptz + (i || ' minutes')::interval, 'gateway-host', 'gateway', "
                            "'telemetry', 'llm_usage', 'info', 'system' FROM generate_series(1, 2000) i"
                        ).format(sql.Identifier(part), sql.Literal(base))
                    )
                cur.execute("ANALYZE events_2026_07")
                cur.execute("ANALYZE events_2026_08")
        conn = psycopg.connect(url, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_threshold_by_shape() -> None:
    assert _threshold_for("events_2026_08_kind_ts_idx") == _TEXT_TS_BYTES_PER_ROW
    assert _threshold_for("events_2026_08_event_name_ts_idx") == _TEXT_TS_BYTES_PER_ROW
    assert _threshold_for("events_2026_08_category_ts_idx") == _TEXT_TS_BYTES_PER_ROW
    assert _threshold_for("events_2026_08_machine_ts_idx") == _TEXT_TS_BYTES_PER_ROW
    assert _threshold_for("events_2026_08_agent_id_ts_idx") == _AGENT_TS_BYTES_PER_ROW
    assert _threshold_for("events_2026_08_pkey") is None  # not governed
    assert _threshold_for("events_2026_08_level_ts_idx") is None


def test_candidates_cover_current_and_previous_month(events_conn: psycopg.Connection) -> None:
    names = [n for n, _bpr in candidate_indexes(events_conn, _NOW)]
    # current + previous month only — June is retention-bound, not governed.
    # (test-schema partitions carry the event_name_ts naming; prod's pre-rename
    # partitions carry kind_ts — both are governed suffixes.)
    assert "events_2026_08_event_name_ts_idx" in names
    assert "events_2026_07_event_name_ts_idx" in names
    assert not any(n.startswith("events_2026_06_") for n in names)
    # the four hot suffixes, and nothing else (no pkey / level / trace_id).
    assert len(names) == 8
    assert all(
        n.endswith(
            (
                "_kind_ts_idx",
                "_event_name_ts_idx",
                "_category_ts_idx",
                "_agent_id_ts_idx",
                "_machine_ts_idx",
            )
        )
        for n in names
    )


def test_pass_reindexes_past_threshold_and_noops_below(
    events_conn: psycopg.Connection,
) -> None:
    # Override 0: every hot index trips -> all reindexed, no errors.
    r0 = reindex_if_bloated(events_conn, _NOW, threshold_override=0.0)
    assert isinstance(r0, ReindexResult)
    assert len(r0.reindexed) == 8
    assert r0.errors == []
    assert r0.checked == 8

    # Override 1e18: nothing trips -> all skipped, nothing reindexed.
    r1 = reindex_if_bloated(events_conn, _NOW, threshold_override=1e18)
    assert r1.reindexed == []
    assert r1.checked == 8
    assert len(r1.skipped_no_bloat) == 8

    # Real thresholds on a healthy index set: 2000 rows keep the fixed page
    # overhead negligible, so the pass is a no-op (the governance loop's
    # steady state).
    r2 = reindex_if_bloated(events_conn, _NOW)
    assert r2.reindexed == []
