"""Whole-response cache + last-good stale serving for the stats-dashboard route.

Entries persist after the fresh TTL expires: besides the 60s hit path, the
route's stale-serving fallback reads the most recent successful response for a
window (`cache_get_last_good`) and serves it marked `stale` (`serve_stale`)
when a recompute fails (task #3973).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any, NamedTuple, cast

from psycopg_pool import ConnectionPool

from gateway.schemas import PluginStat, PluginStatStatus, StatsDashboard, StatsWindowHours
from shared import plugin_stats, telemetry
from shared.config import settings
from shared.events.contract import StatsDashboardStaleReason
from shared.loki_index_labels import ledger_gap_plan, retention_floor

# The sidebar polls every 30 seconds. Caching the complete response for 60
# seconds avoids re-running its roughly 36-query Loki fan-out on every poll.
_CACHE_TTL_S = 60.0
_cache: dict[int, tuple[float, StatsDashboard]] = {}
_cache_lock = threading.Lock()


def _monotonic() -> float:
    """Cache-clock seam, kept local so tests do not alter anyio timing."""
    return time.monotonic()


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


def plugin_stat_rows(pool: ConnectionPool[Any]) -> list[PluginStat]:
    """The runtime values behind plugin-declared statistics cards, for the response.

    Not windowed: a plugin value is a point in time (`PluginStat`), and the
    console joins these rows against the `contributions.ui.stats`
    declarations by `(plugin, id)` — a declared card with no row here renders
    as an explicit empty state.
    """
    return [
        PluginStat(
            plugin=row.plugin,
            id=row.id,
            value=row.value,
            detail=row.detail,
            status=cast(PluginStatStatus, row.status),
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )
        for row in plugin_stats.read_all(pool)
    ]


def cache_get(hours: StatsWindowHours) -> StatsDashboard | None:
    """Return a fresh cached response for the requested window, if present."""
    with _cache_lock:
        hit = _cache.get(int(hours))
        if hit is None or hit[0] + _CACHE_TTL_S <= _monotonic():
            return None
        return hit[1]


def cache_put(hours: StatsWindowHours, response: StatsDashboard) -> None:
    """Store the immutable response after its complete backend read succeeds."""
    with _cache_lock:
        _cache[int(hours)] = (_monotonic(), response)


def cache_get_last_good(hours: StatsWindowHours, *, max_age_s: float) -> StatsDashboard | None:
    """The window's most recent successful response, while within `max_age_s`.

    Deliberately ignores the fresh-cache TTL: this exists for the route's
    stale-serving fallback, which keeps serving the last-good payload after a
    failed recompute until it passes the cap. `max_age_s <= 0` disables the
    fallback (always None); an absent window is also None.
    """
    if max_age_s <= 0:
        return None
    with _cache_lock:
        hit = _cache.get(int(hours))
        if hit is None or _monotonic() - hit[0] > max_age_s:
            return None
        return hit[1]


# ── stale serving (task #3973) ─────────────────────────────────────────────
# When a live recompute fails on a transient Loki error, the route serves the
# window's last-good response (marked `stale`) instead of a 503, for at most
# `display.stats_dashboard_stale_max_s` — so a slow window stops flapping the
# sidebar while a real outage still surfaces as 503 within the cap. One
# `stats_dashboard_stale` event per degradation episode, per-reason rate-capped
# (the `fleet_graph_stale` pattern, task #3925).
_STALE_ROUTE = "/api/stats/dashboard"
_log = logging.getLogger(__name__)

_stale_emit_at: dict[str, float] = {}
_stale_emit_lock = threading.Lock()


def _stale_emit_interval_s() -> float:
    """Seconds between `stats_dashboard_stale` events per reason — settings-backed
    so the storm guard is operator-tunable, and a seam tests use to disable it."""
    return settings.display.stats_dashboard_stale_emit_interval_s


def _emit_stale(reason: StatsDashboardStaleReason) -> None:
    """One `stats_dashboard_stale` event per degradation episode.

    Emitted when GET /api/stats/dashboard serves a last-good response because
    the live recompute failed (Loki transport failure or refused admission).
    `route` is the fixed `_STALE_ROUTE`; `reason` is the closed
    `StatsDashboardStaleReason` set. Rate cap: at most one event per reason per
    `display.stats_dashboard_stale_emit_interval_s` seconds — the event counts
    episodes, not polls or storms.
    """
    now = time.monotonic()
    with _stale_emit_lock:
        last = _stale_emit_at.get(reason)
        if last is not None and now - last < _stale_emit_interval_s():
            return
        _stale_emit_at[reason] = now
    telemetry.emit(
        "telemetry",
        "stats_dashboard_stale",
        level="warning",
        attributes={"route": _STALE_ROUTE, "reason": reason},
    )


def serve_stale(
    hours: StatsWindowHours, *, reason: StatsDashboardStaleReason
) -> StatsDashboard | None:
    """Serve the window's last-good response, marked `stale`, within the age cap.

    Returns None when stale serving is disabled (cap <= 0), nothing is cached
    for the window, or the last-good payload has passed the cap — the caller
    then keeps the route's normal failure contract. Only transient read
    failures call this; the deliberate `ObservabilityReadUnavailable` isolation
    is not a candidate.
    """
    last_good = cache_get_last_good(hours, max_age_s=settings.display.stats_dashboard_stale_max_s)
    if last_good is None:
        return None
    _log.warning(
        "GET /api/stats/dashboard: recompute failed (%s) — serving stale last-good response",
        reason,
    )
    _emit_stale(reason)
    return last_good.model_copy(update={"stale": True})


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

    gap = ledger_gap_plan(newest_day, retention_floor(now))
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
    ledger_from, ledger_to, tail_spans = token_window_plan(window_start, now, newest_day=newest_day)
    ledger = ledger_token_sums(pool, day_from=ledger_from, day_to=ledger_to)
    return ledger or _TokenLedgerSums(0, 0, 0, 0, 0.0), tail_spans
