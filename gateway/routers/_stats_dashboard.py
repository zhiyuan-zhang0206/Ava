"""Whole-response cache for the sidebar stats-dashboard route."""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any, NamedTuple

from psycopg_pool import ConnectionPool

from gateway.schemas import StatsDashboard, StatsWindowHours
from shared.loki_index_labels import ledger_gap_plan, retention_floor

# The sidebar polls every 30 seconds. Caching the complete response for 60
# seconds avoids re-running its roughly 36-query Loki fan-out on every poll.
_CACHE_TTL_S = 60.0
_cache: dict[int, tuple[float, StatsDashboard]] = {}
_cache_lock = threading.Lock()


class _TokenLedgerSums(NamedTuple):
    """Fleet token and cost totals from complete UTC-day ledger rows."""

    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float


def cache_clear() -> None:
    """Test seam: drop all windowed dashboard responses."""
    with _cache_lock:
        _cache.clear()


def cache_get(hours: StatsWindowHours) -> StatsDashboard | None:
    """Return a fresh cached response for the requested window, if present."""
    with _cache_lock:
        hit = _cache.get(int(hours))
        if hit is None or hit[0] + _CACHE_TTL_S <= time.monotonic():
            return None
        return hit[1]


def cache_put(hours: StatsWindowHours, response: StatsDashboard) -> None:
    """Store the immutable response after its complete backend read succeeds."""
    with _cache_lock:
        _cache[int(hours)] = (time.monotonic(), response)


def _utc_midnight(value: datetime | date) -> datetime:
    """Return the UTC midnight beginning ``value``'s calendar day."""
    day = value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    return datetime.combine(day, datetime_time.min, tzinfo=UTC)


def token_window_plan(
    window_start: datetime,
    now: datetime,
    *,
    newest_day: date | None = None,
) -> tuple[date | None, date | None, list[tuple[datetime, datetime]]]:
    """Split dashboard tokens between settled UTC days and Loki tail spans.

    ``newest_day`` is the fleet ledger's global newest day. Its retained
    value is reread from Loki to include any late writes from that closed day.
    """
    if now - window_start <= timedelta(hours=24):
        return None, None, [(window_start, now)]
    window_start_midnight = _utc_midnight(window_start)
    day_from = (
        window_start.date()
        if window_start == window_start_midnight
        else window_start.date() + timedelta(days=1)
    )
    day_to = (now - timedelta(days=1)).date()
    if day_from > day_to:
        return None, None, [(window_start, now)]

    gap = ledger_gap_plan(newest_day, retention_floor())
    if gap.gap_live:
        if gap.day_lt is None:
            raise RuntimeError("live ledger gap requires an exclusive ledger day")
        ledger_day_to = gap.day_lt - timedelta(days=1)
    else:
        ledger_day_to = day_to
    tail_spans: list[tuple[datetime, datetime]] = []
    day_from_midnight = _utc_midnight(day_from)
    if window_start < day_from_midnight:
        tail_spans.append((window_start, day_from_midnight))
    tail_from = max(gap.tail_from, window_start)
    if tail_from < now:
        tail_spans.append((tail_from, now))
    return day_from, ledger_day_to, tail_spans


def _newest_token_ledger_day(pool: ConnectionPool[Any]) -> date | None:
    """Return the fleet's newest token ledger day for the live-tail seam."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(day) FROM agent_model_tokens_daily")
        row = cur.fetchone()
    return row[0] if row is not None else None


def ledger_token_sums(
    pool: ConnectionPool[Any], *, day_from: date | None, day_to: date | None
) -> _TokenLedgerSums | None:
    """Sum fleet token ledger rows in the inclusive complete-day range."""
    if day_from is None and day_to is None:
        return None
    if day_from is None or day_to is None:
        raise ValueError("token ledger bounds must be both set or both None")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(sum(tokens_in), 0), COALESCE(sum(tokens_out), 0), "
            "COALESCE(sum(tokens_cached), 0), COALESCE(sum(tokens_reasoning), 0), "
            "COALESCE(sum(cost_usd), 0) FROM agent_model_tokens_daily "
            "WHERE day >= %s AND day <= %s",
            (day_from, day_to),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("token ledger aggregate returned no row")
    return _TokenLedgerSums(
        tokens_in=int(row[0]),
        tokens_out=int(row[1]),
        tokens_cached=int(row[2]),
        tokens_reasoning=int(row[3]),
        cost_usd=float(row[4]),
    )


def ledger_token_plan(
    pool: ConnectionPool[Any], *, window_start: datetime, now: datetime
) -> tuple[_TokenLedgerSums, list[tuple[datetime, datetime]]]:
    """Load settled token sums and plan their live Loki tail outside the pool."""
    ledger_from, ledger_to, tail_spans = token_window_plan(window_start, now)
    if ledger_from is None:
        return _TokenLedgerSums(0, 0, 0, 0, 0.0), tail_spans
    newest_day = _newest_token_ledger_day(pool)
    ledger_from, ledger_to, tail_spans = token_window_plan(
        window_start, now, newest_day=newest_day
    )
    ledger = ledger_token_sums(pool, day_from=ledger_from, day_to=ledger_to)
    return ledger or _TokenLedgerSums(0, 0, 0, 0, 0.0), tail_spans
