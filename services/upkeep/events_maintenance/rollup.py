"""Day-grain rollup of the telemetry record into the durable ledger tables.

Each maintenance pass recomputes the most recent closed UTC days (yesterday and the days before
it, `RECOMPUTE_DAYS` in all) from `telemetry_events` into:

- ``agent_model_tokens_daily`` — per (agent, day, model): calls, token sums, and the cost
  ledger columns (cost_usd = summed usage-time price snapshots, costed_calls / unpriced_calls).
  Money is summed at usage-time rates, never re-priced. ``estimated_calls`` is never written.
- ``agent_metrics_daily`` — per (agent, day): turn totals/ok/durations, the whole-second turn
  duration histogram and the exec ok/failed split.

A recompute is one idempotent full-day overwrite keyed on the primary key, so a late write (the
mirror replay lands rows up to seven days late) is picked up by the next pass. The ledger rows of
a day that the table only partly holds are protected by a monotone guard: a row is overwritten
only when the recompute has at least as many calls (turns, execs) as the stored one, so a gap in
`telemetry_events` can never lower an existing ledger day. Day boundaries are UTC midnight.

The pass is two SQL statements per day in one short transaction; the day's rows are read in
place, so there is no retention window to clamp to and no per-day watermark.

Operator use, for a range of days (for example after a backfill); a range reaching back past the
fold watermark of `token_totals` also rebuilds `agent_model_tokens_total`:

    .venv/bin/python -m services.upkeep.events_maintenance.rollup --from 20260901 --to 20260930
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import psycopg

from base.db.code_version_gate import ProcessDbGate
from base.events.contract import LLM_USAGE_KEYS, TURN_END_KEYS
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_name
from base.telemetry.event_sql import numeric
from base.telemetry.metrics.aggregate_sql import EXEC_FAILURE_EVENTS

# Late writes arrive up to the mirror's seven-day retention after the event; one more day of
# margin covers a replay that straddles midnight.
RECOMPUTE_DAYS = 8


@dataclass(frozen=True)
class RollupResult:
    """What one `compute_rollup` run attempted. `start_day`/`end_day` are the inclusive UTC-day
    range (None when nothing was attempted); `*_rows` are the upsert row counts."""

    start_day: date | None
    end_day: date | None
    metrics_rows: int
    tokens_rows: int


def _num(expression: str, cast: str = "numeric") -> str:
    return numeric(expression, cast)


_COST = LLM_USAGE_KEYS["cost_usd"]

_TOKENS_SQL = f"""
    INSERT INTO agent_model_tokens_daily
        (agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached,
         tokens_reasoning, cost_usd, costed_calls, unpriced_calls)
    SELECT t.agent_id, %(day)s, COALESCE({LLM_USAGE_KEYS["model"]}, ''), count(*),
           COALESCE(sum({_num(LLM_USAGE_KEYS["in_total"])}), 0)::bigint,
           COALESCE(sum({_num(LLM_USAGE_KEYS["out_total"])}), 0)::bigint,
           COALESCE(sum({_num(LLM_USAGE_KEYS["cache_read"])}), 0)::bigint,
           COALESCE(sum({_num(LLM_USAGE_KEYS["reasoning"])}), 0)::bigint,
           COALESCE(sum({_num(_COST)}), 0)::float8,
           count(*) FILTER (WHERE COALESCE({_COST}, '') <> ''),
           count(*) FILTER (WHERE COALESCE({_COST}, '') = '')
    FROM telemetry_events t
    JOIN agents a ON a.id = t.agent_id
    WHERE t.ts >= %(start)s AND t.ts < %(end)s AND t.event_name = 'llm_usage'
    GROUP BY t.agent_id, 3
    ON CONFLICT (agent_id, day, model) DO UPDATE SET
        llm_calls        = EXCLUDED.llm_calls,
        tokens_in        = EXCLUDED.tokens_in,
        tokens_out       = EXCLUDED.tokens_out,
        tokens_cached    = EXCLUDED.tokens_cached,
        tokens_reasoning = EXCLUDED.tokens_reasoning,
        cost_usd         = EXCLUDED.cost_usd,
        costed_calls     = EXCLUDED.costed_calls,
        unpriced_calls   = EXCLUDED.unpriced_calls
    WHERE EXCLUDED.llm_calls >= agent_model_tokens_daily.llm_calls
"""  # noqa: S608 — keys come from the registered payload constants

_METRICS_SQL = f"""
    WITH f AS (
        SELECT agent_id, event_name, {_num(TURN_END_KEYS["duration_seconds"], "float8")} AS dur, {TURN_END_KEYS["ok"]} AS ok
        FROM telemetry_events
        WHERE ts >= %(start)s AND ts < %(end)s AND agent_id IS NOT NULL
          AND (event_name IN ('turn_end', 'exec') OR event_name = ANY(%(fail)s))
    ), turn AS (
        SELECT agent_id, count(*) AS turn_total, count(*) FILTER (WHERE ok = 'true') AS turn_ok,
               COALESCE(sum(dur), 0) AS dsum, min(dur) AS dmin, max(dur) AS dmax
        FROM f WHERE event_name = 'turn_end' GROUP BY agent_id
    ), hist AS (
        SELECT agent_id, jsonb_object_agg(b::text, n) AS h
        FROM (SELECT agent_id, floor(dur)::bigint AS b, count(*) AS n
              FROM f WHERE event_name = 'turn_end' AND dur IS NOT NULL GROUP BY 1, 2) x
        GROUP BY agent_id
    ), ex AS (
        SELECT agent_id, count(*) FILTER (WHERE event_name = 'exec') AS ok,
               count(*) FILTER (WHERE event_name <> 'exec') AS failed
        FROM f WHERE event_name <> 'turn_end' GROUP BY agent_id
    )
    INSERT INTO agent_metrics_daily
        (agent_id, day, turn_total, turn_ok, turn_dur_sum, turn_dur_min, turn_dur_max,
         turn_dur_hist, exec_ok, exec_failed)
    SELECT a.id, %(day)s, COALESCE(turn.turn_total, 0), COALESCE(turn.turn_ok, 0),
           COALESCE(turn.dsum, 0), turn.dmin, turn.dmax, COALESCE(hist.h, '{{}}'::jsonb),
           COALESCE(ex.ok, 0), COALESCE(ex.failed, 0)
    FROM agents a
    JOIN (SELECT agent_id FROM turn UNION SELECT agent_id FROM ex) k ON k.agent_id = a.id
    LEFT JOIN turn ON turn.agent_id = a.id
    LEFT JOIN hist ON hist.agent_id = a.id
    LEFT JOIN ex ON ex.agent_id = a.id
    ON CONFLICT (agent_id, day) DO UPDATE SET
        turn_total    = EXCLUDED.turn_total,
        turn_ok       = EXCLUDED.turn_ok,
        turn_dur_sum  = EXCLUDED.turn_dur_sum,
        turn_dur_min  = EXCLUDED.turn_dur_min,
        turn_dur_max  = EXCLUDED.turn_dur_max,
        turn_dur_hist = EXCLUDED.turn_dur_hist,
        exec_ok       = EXCLUDED.exec_ok,
        exec_failed   = EXCLUDED.exec_failed
    WHERE EXCLUDED.turn_total >= agent_metrics_daily.turn_total
      AND EXCLUDED.exec_ok + EXCLUDED.exec_failed
          >= agent_metrics_daily.exec_ok + agent_metrics_daily.exec_failed
"""  # noqa: S608 — keys come from the registered payload constants


def roll_day(conn: psycopg.Connection, day: date) -> tuple[int, int]:
    """Recompute one closed UTC day; returns `(metrics_rows, tokens_rows)` written."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    params = {
        "day": day,
        "start": start,
        "end": start + timedelta(days=1),
        "fail": EXEC_FAILURE_EVENTS,
    }
    with conn.transaction():
        tokens = conn.execute(_TOKENS_SQL, params).rowcount  # type: ignore[arg-type]
        metrics = conn.execute(_METRICS_SQL, params).rowcount  # type: ignore[arg-type]
    return max(metrics, 0), max(tokens, 0)


def roll_days(conn: psycopg.Connection, first: date, last: date) -> RollupResult:
    """Recompute every closed UTC day in `[first, last]`, oldest first."""
    metrics_rows = tokens_rows = 0
    day = first
    while day <= last:
        metrics, tokens = roll_day(conn, day)
        metrics_rows += metrics
        tokens_rows += tokens
        day += timedelta(days=1)
    return RollupResult(first, last, metrics_rows, tokens_rows)


def compute_rollup(
    conn: psycopg.Connection, *, now_utc: datetime, lookback_days: int = RECOMPUTE_DAYS
) -> RollupResult:
    """Recompute the last `lookback_days` closed UTC days (yesterday and the days before it)."""
    yesterday = now_utc.astimezone(UTC).date() - timedelta(days=1)
    return roll_days(conn, yesterday - timedelta(days=lookback_days - 1), yesterday)


def _day_arg(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC).date()


def main(argv: list[str] | None = None) -> int:
    """Operator CLI: recompute a range of closed UTC days (monotone guard applies)."""
    from services.upkeep.events_maintenance.daemon import events_maintenance_db

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--from", dest="first", type=_day_arg, required=True, metavar="YYYYMMDD")
    parser.add_argument("--to", dest="last", type=_day_arg, required=True, metavar="YYYYMMDD")
    args = parser.parse_args(argv)
    if args.first > args.last:
        parser.error("--from is after --to")
    from services.upkeep.events_maintenance.token_totals import folded_through, rebuild_totals

    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process=process_name())
    with events_maintenance_db(gate=gate).connect() as conn:
        result = roll_days(conn, args.first, args.last)
        # A day at or before the watermark is already inside the folded totals.
        if args.first <= folded_through(conn):
            rebuild_totals(conn, today=datetime.now(UTC).date())
            sys.stdout.write("rebuilt agent_model_tokens_total\n")
    sys.stdout.write(
        f"rolled {result.start_day}..{result.end_day}: "
        f"{result.tokens_rows} token rows, {result.metrics_rows} metrics rows\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
