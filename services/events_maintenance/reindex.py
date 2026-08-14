"""Events partition index-bloat governance (audit Round 2, M2 / P1-2 ①).

The `events` partitions are append-mostly and hammered by wide-window index
scans (the kind_ts index alone had 5.8B idx_tup_read across 393K scans on the
2026-08 partition); their btree indexes bloat without bound — measured 51-73
bytes/row on prod 2026-08-08 (a ~1.5-2x expansion over healthy), and bloat is
exactly what turns the parallel index-only scan into the 60s+ timeout
anti-pattern of P1-2. `reindex_if_bloated` adds the missing governance to the
events-maintenance daemon's hourly pass: estimate bloat, and
`REINDEX INDEX CONCURRENTLY` the hot indexes that tripped.

Bloat is estimated WITHOUT pgstattuple (not installed, and the cluster role is
NOSUPERUSER — it cannot CREATE EXTENSION): bytes-per-row of each partition
index vs a per-shape threshold. Calibration (prod, 2026-08-08, measured right
after a REINDEX): text+ts indexes (kind/category/machine) ≈ 35 B/row,
agent_id+ts ≈ 31 B/row; the thresholds sit at ~1.5x those fresh values, so a
partition index needs to roughly double before it trips — a no-op pass on a
healthy cluster.

REINDEX CONCURRENTLY cannot run inside a transaction block and its internal
phases would be split across backends by the transaction-mode PgBouncer, so
this module dials the DIRECT Postgres URL with autocommit — the same
exemption the migration applier uses (shared/db.connect(direct=True)).

Only the current and previous UTC-month partitions are considered: older
partitions are retention-bound anyway and the audit's hot set is the write
frontier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

import shared.db
from shared.log import logger

# The four query-pattern indexes the reads actually hammer (schema.sql's
# "Query-pattern indexes" block); per-partition copies carry these suffixes.
# `_kind_ts_idx` is the legacy name (partitions born before the 2026-08-04
# unified-events migration renamed the parent index); partitions created after
# it carry `_event_name_ts_idx`. Both are governed — the name is cosmetic, the
# shape is the same (event_name, ts DESC).
_HOT_SUFFIXES = (
    "_kind_ts_idx",
    "_event_name_ts_idx",
    "_category_ts_idx",
    "_agent_id_ts_idx",
    "_machine_ts_idx",
)
# Thresholds in bytes per estimated row (see module docstring for calibration).
_TEXT_TS_BYTES_PER_ROW = 60.0  # kind / category / machine (text + timestamptz)
_AGENT_TS_BYTES_PER_ROW = 45.0  # agent_id (bigint + timestamptz)


@dataclass
class ReindexResult:
    """One governance pass: which hot indexes were rebuilt, and why not."""

    reindexed: list[str]
    checked: int
    skipped_no_bloat: list[str]
    errors: list[str]

    def summary(self) -> str:
        if not self.reindexed and not self.errors:
            return ""
        parts: list[str] = []
        if self.reindexed:
            parts.append(f"reindexed {', '.join(self.reindexed)}")
        if self.errors:
            parts.append(f"errors {', '.join(self.errors)}")
        return "; ".join(parts)


def _threshold_for(index_name: str) -> float | None:
    """Bloat threshold (bytes/row) for one hot index shape; None = not governed."""
    if index_name.endswith("_agent_id_ts_idx"):
        return _AGENT_TS_BYTES_PER_ROW
    if index_name.endswith(
        ("_kind_ts_idx", "_event_name_ts_idx", "_category_ts_idx", "_machine_ts_idx")
    ):
        return _TEXT_TS_BYTES_PER_ROW
    return None


def candidate_indexes(conn: psycopg.Connection, now_utc: datetime) -> list[tuple[str, int]]:
    """(index_name, bytes_per_row) for the hot indexes of the current and
    previous UTC-month `events` partitions.

    `pg_relation_size` is the live physical size; `reltuples` is the planner's
    row estimate (fresh enough for a 1.5x-threshold decision — ANALYZE runs on
    the partitions by autovacuum and the maintenance pass). Indexes on the
    DEFAULT partition (no month suffix) are excluded by the name pattern.
    """
    months = [
        f"events_{now_utc.strftime('%Y_%m')}",
        f"events_{(now_utc.replace(day=1) - timedelta(days=1)).strftime('%Y_%m')}",
    ]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.relname,
                   pg_relation_size(i.oid) / NULLIF(GREATEST(t.reltuples, 1), 0) AS bytes_per_row
            FROM pg_class i
            JOIN pg_namespace n ON n.oid = i.relnamespace
            JOIN pg_index ix ON ix.indexrelid = i.oid
            JOIN pg_class t ON t.oid = ix.indrelid
            WHERE n.nspname = 'public'
              AND t.relname = ANY(%s)
              AND i.relname LIKE 'events\\_%%\\_idx'
            ORDER BY i.relname
            """,
            (months,),
        )
        # Keep only the governed (hot) shapes — the broad LIKE also catches
        # pkey / trace_id / level children, which never bloat enough to matter.
        return [
            (name, int(bpr)) for name, bpr in cur.fetchall() if _threshold_for(name) is not None
        ]


def reindex_if_bloated(
    conn: psycopg.Connection,
    now_utc: datetime,
    *,
    threshold_override: float | None = None,
) -> ReindexResult:
    """One governance pass: REINDEX CONCURRENTLY every hot index whose
    estimated bytes/row exceeds its shape threshold.

    `conn` MUST be a direct autocommit connection (see module docstring). A
    failed REINDEX is logged and collected — one bad index must not abort the
    rest of the pass or the daemon's maintenance round. `threshold_override`
    replaces every shape threshold (tests force both branches deterministically
    without manufacturing a real bloated index).
    """
    result = ReindexResult(reindexed=[], checked=0, skipped_no_bloat=[], errors=[])
    for name, bpr in candidate_indexes(conn, now_utc):
        threshold = threshold_override if threshold_override is not None else _threshold_for(name)
        if threshold is None:
            continue
        result.checked += 1
        if bpr <= threshold:
            result.skipped_no_bloat.append(f"{name}({bpr}B/row)")
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("REINDEX INDEX CONCURRENTLY {}").format(sql.Identifier(name)))
            result.reindexed.append(name)
            logger.info(
                "[events-maintenance] reindexed %s (%.0f B/row > %.0f threshold)",
                name,
                bpr,
                threshold,
            )
        except psycopg.Error:
            logger.exception("[events-maintenance] REINDEX CONCURRENTLY failed for %s", name)
            result.errors.append(name)
    return result


def run_governance_pass(now_utc: datetime | None = None) -> ReindexResult:
    """Entry point for the daemon: open a direct autocommit connection and run
    one pass. Callers (tests) can inject a connection via `reindex_if_bloated`."""
    now = now_utc or datetime.now(tz=UTC)
    with shared.db.connect(direct=True, autocommit=True) as conn:
        return reindex_if_bloated(conn, now)
