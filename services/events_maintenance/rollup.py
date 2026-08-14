# ruff: noqa: S608 — SQL constants interpolate only registry-derived key fragments (shared/events/contract.py); all values ride %(name)s placeholders
"""Since-Birth rollup — recompute the day-grain aggregates from `events`.

The single source of truth for the rollup aggregation SQL (backfill and
steady-state are the same code path — a fresh cluster whose rollup tables are
empty backfills all history; a live cluster re-rolls only the recent tail). The
daemon (`services.events_maintenance.daemon`) calls `compute_rollup` on its poll
loop; tests call it directly against a testcontainers DB.

Day boundary is UTC midnight. The rollup covers only *whole* days up to
yesterday — today is still being written, so the since-birth readers serve it
live and stitch `rollup WHERE day < today` + `raw rows WHERE ts >= today 00:00
UTC` (PR3). The two upserts must therefore stop exactly at today's UTC midnight,
which is why the input window's exclusive upper bound is `today 00:00 UTC`.

The upsert is a **full-day overwrite recompute** keyed on the PK — it re-derives
each day's aggregate from the raw rows and overwrites, so a re-run never
double-counts (unlike an incremental accumulate). The `events` stream is append-only, so
a recompute can only add rows or raise a group's totals, never orphan a stale
rollup row; upsert-without-delete is therefore complete. Each run also re-rolls
the last `lookback_days` already-rolled days so a late write into a closed day is
picked up.

Field extraction mirrors the live readers exactly (so PR3 can diff the rollup
against the pre-change numbers):
  - tokens  ← llm_usage payload: model / in_total / out_total / cache_read /
              reasoning   (see agent_inspect._agent_cost, fleet_graph total_tokens)
  - turns   ← turn_end payload: ok / duration_seconds
  - exec_ok ← event_name = 'exec'; exec_failed ← event_name LIKE 'exec_%' / 'exec(%'
              (see agent_inspect._agent_stats, the metrics report's exec split)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import LiteralString, cast

import psycopg
from psycopg import sql

from shared.events.contract import LLM_USAGE_KEYS, TURN_END_KEYS

# tokens: per (agent, UTC-day, model). model '' folds the llm_usage rows with no
# model field (COALESCE), matching how the cost reader groups the `model` key.
# Bounded by ts (sargable on the (event, ts DESC) index) then grouped by UTC day.
_TOKENS_UPSERT: LiteralString = cast(
    LiteralString,
    f"""
    INSERT INTO agent_model_tokens_daily
        (agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached, tokens_reasoning)
    SELECT agent_id,
           (ts AT TIME ZONE 'UTC')::date AS day,
           COALESCE({LLM_USAGE_KEYS["model"]}, '') AS model,
           COUNT(*),
           COALESCE(SUM(({LLM_USAGE_KEYS["in_total"]})::bigint), 0),
           COALESCE(SUM(({LLM_USAGE_KEYS["out_total"]})::bigint), 0),
           COALESCE(SUM(({LLM_USAGE_KEYS["cache_read"]})::bigint), 0),
           COALESCE(SUM(({LLM_USAGE_KEYS["reasoning"]})::bigint), 0)
    FROM events
    WHERE event_name = 'llm_usage'
      AND agent_id IS NOT NULL
      AND ts >= %(start_ts)s AND ts < %(end_ts)s
    GROUP BY agent_id, (ts AT TIME ZONE 'UTC')::date, COALESCE({LLM_USAGE_KEYS["model"]}, '')
    ON CONFLICT (agent_id, day, model) DO UPDATE SET
        llm_calls        = EXCLUDED.llm_calls,
        tokens_in        = EXCLUDED.tokens_in,
        tokens_out       = EXCLUDED.tokens_out,
        tokens_cached    = EXCLUDED.tokens_cached,
        tokens_reasoning = EXCLUDED.tokens_reasoning
""",
)

# metrics: per (agent, UTC-day). One scan over turn_end + exec-family rows, split
# by conditional FILTER. exec_failed = every exec outcome other than plain 'exec'
# (exec_failed / exec_thread_stuck / exec(timeout)); `\_` escapes LIKE's `_`
# wildcard and `%%` is a literal `%` (psycopg placeholder escaping). Raw string so
# Python does not choke on `\_`.
_METRICS_UPSERT: LiteralString = cast(
    LiteralString,
    rf"""
    INSERT INTO agent_metrics_daily
        (agent_id, day, turn_total, turn_ok, turn_dur_sum, turn_dur_min, turn_dur_max,
         exec_ok, exec_failed)
    SELECT agent_id,
           (ts AT TIME ZONE 'UTC')::date AS day,
           COUNT(*) FILTER (WHERE event_name = 'turn_end'),
           COUNT(*) FILTER (WHERE event_name = 'turn_end' AND {TURN_END_KEYS["ok"]} = 'true'),
           COALESCE(SUM(({TURN_END_KEYS["duration_seconds"]})::float)
                    FILTER (WHERE event_name = 'turn_end'), 0),
           MIN(({TURN_END_KEYS["duration_seconds"]})::float) FILTER (WHERE event_name = 'turn_end'),
           MAX(({TURN_END_KEYS["duration_seconds"]})::float) FILTER (WHERE event_name = 'turn_end'),
           COUNT(*) FILTER (WHERE event_name = 'exec'),
           COUNT(*) FILTER (WHERE event_name LIKE 'exec\_%%' OR event_name LIKE 'exec(%%')
    FROM events
    WHERE agent_id IS NOT NULL
      AND (event_name = 'turn_end' OR event_name = 'exec'
           OR event_name LIKE 'exec\_%%' OR event_name LIKE 'exec(%%')
      AND ts >= %(start_ts)s AND ts < %(end_ts)s
    GROUP BY agent_id, (ts AT TIME ZONE 'UTC')::date
    ON CONFLICT (agent_id, day) DO UPDATE SET
        turn_total   = EXCLUDED.turn_total,
        turn_ok      = EXCLUDED.turn_ok,
        turn_dur_sum = EXCLUDED.turn_dur_sum,
        turn_dur_min = EXCLUDED.turn_dur_min,
        turn_dur_max = EXCLUDED.turn_dur_max,
        exec_ok      = EXCLUDED.exec_ok,
        exec_failed  = EXCLUDED.exec_failed
""",
)


@dataclass(frozen=True)
class RollupResult:
    """What one `compute_rollup` run covered. `start_day`/`end_day` are the
    inclusive UTC-day range recomputed (None when the run was a no-op — no whole
    day to roll yet); `*_rows` are the upsert row counts (inserted + updated)."""

    start_day: date | None
    end_day: date | None
    metrics_rows: int
    tokens_rows: int


def _max_rolled_day(cur: psycopg.Cursor, table: str) -> date | None:
    """The newest `day` already rolled into `table` (None = empty). `table` is a
    fixed internal literal; composed via sql.Identifier for safety + typing."""
    cur.execute(sql.SQL("SELECT max(day) FROM {}").format(sql.Identifier(table)))
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — aggregate without GROUP BY always returns one row
    return row[0]


def _earliest_event_day(cur: psycopg.Cursor) -> date | None:
    """The earliest UTC day carrying an agent-scoped rollup-relevant event
    (None = no such rows). Used only for the empty-table backfill lower bound so
    the first run does not scan from an arbitrary epoch."""
    cur.execute(
        r"SELECT MIN((ts AT TIME ZONE 'UTC')::date) FROM events "
        r"WHERE agent_id IS NOT NULL "
        r"  AND (event_name = 'llm_usage' OR event_name = 'turn_end' OR event_name = 'exec' "
        r"       OR event_name LIKE 'exec\_%%' OR event_name LIKE 'exec(%%')"
    )
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — aggregate without GROUP BY always returns one row
    return row[0]


def compute_rollup(
    conn: psycopg.Connection, *, now_utc: datetime, lookback_days: int
) -> RollupResult:
    """Recompute the rollup tables for the whole days up to yesterday (UTC).

    Range: `[start_day, yesterday]` where yesterday = `now_utc`'s UTC date minus
    one day, and `start_day` is either the earliest event day (empty tables →
    full backfill) or `min(max rolled day) - lookback_days` (steady state). The
    end is always yesterday and the start is anchored to the last rolled day, so a
    downtime gap of any length is fully recovered on the next run — the lookback
    only adds slack for late writes into already-closed days.

    The watermark reads and both upserts run in one transaction (atomic
    maintenance run), which also guarantees a clean commit independent of the
    connection's autocommit mode — the caller passes a connection with no open
    transaction (a fresh pool connection; the daemon's contract). Idempotent.
    """
    today = now_utc.astimezone(UTC).date()
    yesterday = today - timedelta(days=1)

    with conn.transaction(), conn.cursor() as cur:
        max_metrics = _max_rolled_day(cur, "agent_metrics_daily")
        max_tokens = _max_rolled_day(cur, "agent_model_tokens_daily")
        # Progress marker = the furthest day either table has reached. Both upserts
        # run over the SAME range in ONE transaction, so a lower max in one table
        # reflects that family's data sparsity (a day with llm_usage but no turns,
        # or vice versa), NOT an unprocessed gap — the days were processed, they
        # just produced no row for that family. So a table that is legitimately
        # empty forever (a tokens-only / metrics-only workload) must not drag us
        # back into a full-history rescan every run: take the max, not the min.
        processed = [d for d in (max_metrics, max_tokens) if d is not None]
        if not processed:
            # Nothing processed yet → initial backfill from the earliest event day.
            start_day = _earliest_event_day(cur)
            if start_day is None:
                return RollupResult(None, None, 0, 0)
        else:
            start_day = max(processed) - timedelta(days=lookback_days)

        if start_day > yesterday:
            # Nothing has completed a whole day yet (brand-new cluster / only
            # today's data), so there is nothing to roll.
            return RollupResult(None, None, 0, 0)

        start_ts = datetime.combine(start_day, time.min, tzinfo=UTC)
        end_ts = datetime.combine(today, time.min, tzinfo=UTC)  # exclusive: today 00:00 UTC
        params = {"start_ts": start_ts, "end_ts": end_ts}

        cur.execute(_TOKENS_UPSERT, params)
        tokens_rows = cur.rowcount
        cur.execute(_METRICS_UPSERT, params)
        metrics_rows = cur.rowcount

    return RollupResult(start_day, yesterday, metrics_rows, tokens_rows)
