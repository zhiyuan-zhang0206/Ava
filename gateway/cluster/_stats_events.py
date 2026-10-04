"""The stats dashboard's window totals, read from `telemetry_events`.

One scan of the window's `llm_usage` and `turn_end` rows gives the token, cost and turn
sums. The cluster filter accepts the home cluster's label and rows with no label, as every
dashboard read does. The warning/error classes are `gateway.cluster.alert_classes`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

import psycopg

from base.events.contract import LLM_USAGE_KEYS, TURN_END_KEYS
from base.telemetry.event_sql import numeric


class WindowTotals(NamedTuple):
    """Token, cost and turn sums over one window."""

    in_total: int
    out_total: int
    cache_read: int
    cost_usd: float
    turn_seconds: float
    turn_count: int


def window_totals(
    conn: psycopg.Connection[Any], *, cluster: str, start: datetime, end: datetime
) -> WindowTotals:
    """Tokens, cost and turn duration over `(start, end]`.

    Cost is the usage-time snapshot each row carries, never today's registry price. A value
    that is missing or not a number is left out of its sum; the turn count is the number of
    successful `turn_end` rows, with or without a duration.
    """
    usage = "event_name = 'llm_usage' AND category = 'telemetry'"
    turn = f"event_name = 'turn_end' AND {TURN_END_KEYS['ok']} = 'true'"
    query = f"""
        SELECT
          COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["out_total"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["cache_read"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["cost_usd"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(TURN_END_KEYS["duration_seconds"])}) FILTER (WHERE {turn}), 0),
          count(*) FILTER (WHERE {turn})
        FROM telemetry_events
        WHERE event_name IN ('llm_usage', 'turn_end')
          AND (cluster = %s OR cluster = '')
          AND ts > %s AND ts <= %s
    """  # noqa: S608 — keys come from the registered payload constants
    row = conn.execute(query, (cluster, start, end)).fetchone()  # type: ignore[arg-type]
    assert row is not None  # noqa: S101 — an aggregate always returns one row
    return WindowTotals(
        in_total=round(row[0]),
        out_total=round(row[1]),
        cache_read=round(row[2]),
        cost_usd=float(row[3]),
        turn_seconds=float(row[4]),
        turn_count=int(row[5]),
    )
