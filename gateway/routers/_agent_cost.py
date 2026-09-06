"""Per-agent cost aggregation — the daily ledger + a bounded Loki tail.

``agent_model_tokens_daily`` is the durable cost ledger: whole UTC days,
written by the events-maintenance Loki rollup pass (and, for pre-LGTM
history, backfilled once by the llm-cost-rollup-columns migration — the
archive's last code reader). Cost is never re-priced: each row's
``cost_usd`` is the sum of usage-time price snapshots; calls without a
snapshot count in ``unpriced_calls`` and contribute 0.

Reads take one of two bounded paths:

- a windowed request (``hours`` requested up to 7d by the StatsWindowHours
  whitelist and clamped to Loki retention, or ``since_compact`` whose halt
  marker can only be found inside Loki retention anyway) aggregates **pure
  Loki** over the window — with 24h query splitting + result caches this is a
  handful of subqueries;
- whole life = **ledger** (every rolled day except the newest retained day,
  which can be stale after a late write) + **Loki tail** from the reduced
  ledger watermark. Older days remain ledger-served; the newest retained day
  lives entirely in the tail, so it is neither lost nor double counted.

A small TTL cache absorbs the inspector's poll cadence: the 5s poll costs
one recompute per ``_CACHE_TTL_S`` per (agent, window) instead of five
whole aggregations per second.
"""

from __future__ import annotations

import threading
import time as time_mod
from datetime import UTC, date, datetime
from typing import Any, TypedDict

from psycopg import sql
from psycopg_pool import ConnectionPool

from gateway import loki_events
from gateway.schemas import AgentCost, StatsWindowHours, applied_window
from shared.loki_index_labels import ledger_gap_plan, retention_floor

# The compact-halt event name (payload key `body` mentions "compact") —
# mirrors `HALT_KEYS` in shared/events/contract.py.
HALT_EVENT = "halt"

_CACHE_TTL_S = 30.0
_INSPECT_QUERY_TIMEOUT_S = 8.0


def _query_timeout(deadline: float | None) -> float | None:
    """Return the remaining per-query budget, capped for interactive inspect."""
    if deadline is None:
        return None
    remaining = deadline - time_mod.monotonic()
    if remaining <= 0.1:
        return 0.1
    return min(_INSPECT_QUERY_TIMEOUT_S, remaining)


class _AggCommon(TypedDict, total=False):
    """Shared filter kwargs for the loki_events aggregate calls — a TypedDict so
    `**common` unpacks with exact types under pyright strict."""

    event_names: list[str]
    categories: list[str]
    agent_id: int
    from_: datetime | None
    to: datetime | None


def window_bounds(
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
    deadline: float | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Resolve (from_, window_start) for the Loki-side event aggregates
    (stats / activity / TPS — bounded readers). ``since_compact`` wins: the
    agent's latest compact-halt ts (None = never compacted -> retention
    floor). Otherwise ``hours`` -> now - the selected window; neither -> None (the inspect
    path resolves whole life through its PG ledger/archive and a retained Loki
    tail; the cost path has its own matching ledger read). ``window_start`` is
    the same instant, None when the request is whole-life (the alive-time clip
    treats None as since-birth)."""
    if since_compact:
        compact_ts = _compact_ts(agent_id, deadline=deadline)
        if compact_ts is None:
            return retention_floor(), None
        return compact_ts, compact_ts
    if hours is not None:
        _applied, duration = applied_window(hours)
        window_from = datetime.now(tz=UTC) - duration
        return window_from, window_from
    return None, None


def _compact_ts(agent_id: int, *, deadline: float | None = None) -> datetime | None:
    """The agent's latest `halt` event whose body mentions compact (the
    system_halt emitted when its history is compacted) — the since-compact
    window lower bound; None when no such event exists within Loki
    retention (never compacted, or compacted longer ago than retention —
    indistinguishable, both read as whole-life)."""
    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=[HALT_EVENT],
        grep="compact",
        from_=retention_floor(),
        limit=1,
        timeout_s=_query_timeout(deadline),
    )
    return rows[0]["ts"] if rows else None


class _ModelAgg:
    """Mutable per-model accumulator merged across the ledger and Loki tail."""

    __slots__ = (
        "calls",
        "cost",
        "costed_calls",
        "tcached",
        "tin",
        "tout",
        "treason",
        "unpriced_calls",
    )

    def __init__(self) -> None:
        self.calls = 0
        self.costed_calls = 0
        self.unpriced_calls = 0
        self.tin = 0
        self.tout = 0
        self.tcached = 0
        self.treason = 0
        self.cost = 0.0


_LEDGER_SQL = sql.SQL("""SELECT model,
  sum(llm_calls), sum(costed_calls), sum(unpriced_calls),
  sum(tokens_in), sum(tokens_out), sum(tokens_cached), sum(tokens_reasoning),
  sum(cost_usd)
FROM agent_model_tokens_daily
WHERE agent_id = %s{}
GROUP BY model""")


def _max_token_day(pool: ConnectionPool[Any], agent_id: int) -> date | None:
    """Return the newest durable token-ledger day for one agent."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT max(day) FROM agent_model_tokens_daily WHERE agent_id = %s", (agent_id,)
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def _ledger_aggs(
    pool: ConnectionPool[Any], agent_id: int, *, day_lt: date | None = None
) -> dict[str, _ModelAgg]:
    """Per-model sums over rolled ledger days.

    ``day_lt`` excludes the retained gap day; `ledger_gap_plan` determines
    the matching live-tail start independently of older ledger rows.
    """
    out: dict[str, _ModelAgg] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        condition = sql.SQL(" AND day < %s") if day_lt is not None else sql.SQL("")
        params = (agent_id, day_lt) if day_lt is not None else (agent_id,)
        cur.execute(_LEDGER_SQL.format(condition), params)
        for row in cur.fetchall():
            agg = out.setdefault(row[0], _ModelAgg())
            agg.calls = int(row[1])
            agg.costed_calls = int(row[2])
            agg.unpriced_calls = int(row[3])
            agg.tin = int(row[4])
            agg.tout = int(row[5])
            agg.tcached = int(row[6])
            agg.treason = int(row[7])
            agg.cost = float(row[8])
    return out


def _loki_aggs_into(
    merged: dict[str, _ModelAgg],
    agent_id: int,
    from_: datetime,
    to: datetime | None,
    *,
    deadline: float | None = None,
) -> None:
    """Accumulate per-model llm_usage aggregates from Loki over [from_, to)
    into ``merged``. Rows carrying a cost snapshot sum it directly; rows
    without one are unpriced (0 cost) — no read-time re-pricing. A pinned
    ``to`` makes the per-model sums and counts a consistent snapshot: every
    query is evaluated at the same instant, with no cross-query skew at the
    live tail."""
    if to is None:
        to = datetime.now(tz=UTC)
    common: _AggCommon = {
        "event_names": ["llm_usage"],
        "categories": ["telemetry", "log"],
        "agent_id": agent_id,
        "from_": from_,
        "to": to,
    }
    sums = {
        key: dict(
            loki_events.attribute_aggregate(
                field=key,
                agg="sum",
                group_by="model",
                timeout_s=_query_timeout(deadline),
                **common,
            )
        )
        for key in ("in_total", "out_total", "cache_read", "reasoning", "cost_usd")
    }
    calls = dict(
        loki_events.attribute_aggregate(
            field="in_total",
            agg="count",
            group_by="model",
            timeout_s=_query_timeout(deadline),
            **common,
        )
    )
    costed = dict(
        loki_events.attribute_aggregate(
            field="in_total",
            agg="count",
            group_by="model",
            attribute_filters={"cost_usd": "!="},
            timeout_s=_query_timeout(deadline),
            **common,
        )
    )
    for model in calls:
        agg = merged.setdefault(model, _ModelAgg())
        n_calls = int(calls.get(model, 0))
        n_costed = int(costed.get(model, 0))
        agg.calls += n_calls
        agg.costed_calls += n_costed
        agg.unpriced_calls += n_calls - n_costed
        agg.tin += int(sums["in_total"].get(model, 0.0))
        agg.tout += int(sums["out_total"].get(model, 0.0))
        agg.tcached += int(sums["cache_read"].get(model, 0.0))
        agg.treason += int(sums["reasoning"].get(model, 0.0))
        agg.cost += float(sums["cost_usd"].get(model, 0.0))


def _to_agent_cost(merged: dict[str, _ModelAgg]) -> AgentCost:
    tin = tout = tcached = treason = calls = unpriced = 0
    total_cost = 0.0
    for agg in merged.values():
        tin += agg.tin
        tout += agg.tout
        tcached += agg.tcached
        treason += agg.treason
        calls += agg.calls
        unpriced += agg.unpriced_calls
        total_cost += agg.cost
    # These are AgentCost's declared domains. The pinned snapshot makes them
    # hold by construction; this last defense keeps cache_read without in_total
    # or an out-of-domain ledger row from turning the inspector report into a 503.
    unpriced_calls = max(0, unpriced)
    cache_hit_pct = min(100.0, round(tcached / tin * 100, 2)) if tin else 0.0
    return AgentCost(
        cost_usd=round(total_cost, 4),
        unpriced_calls=unpriced_calls,
        llm_calls=calls,
        tokens_in=tin,
        tokens_out=tout,
        tokens_cached=tcached,
        tokens_reasoning=treason,
        cache_hit_pct=cache_hit_pct,
    )


# (agent_id, hours, since_compact) -> (monotonic expiry, AgentCost). Guarded
# by a lock — agent_cost runs inside asyncio.to_thread workers.
_cache: dict[tuple[int, int | None, bool], tuple[float, AgentCost]] = {}
_cache_lock = threading.Lock()


def agent_cost(
    pool: ConnectionPool[Any],
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
    deadline: float | None = None,
) -> AgentCost:
    """LLM cost + token totals for one agent — see the module docstring for
    the two read paths. Serves from the TTL cache within ``_CACHE_TTL_S``."""
    key = (agent_id, int(hours) if hours is not None else None, since_compact)
    now_mono = time_mod.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now_mono:
            return hit[1]

    windowed_from: datetime | None = None
    if since_compact:
        windowed_from = _compact_ts(
            agent_id, deadline=deadline
        )  # None = never compacted -> whole life
    elif hours is not None:
        _applied, duration = applied_window(hours)
        windowed_from = datetime.now(tz=UTC) - duration

    merged: dict[str, _ModelAgg] = {}
    if windowed_from is not None:
        _loki_aggs_into(merged, agent_id, windowed_from, None, deadline=deadline)
    else:
        max_day = _max_token_day(pool, agent_id)
        floor = retention_floor()
        gap = ledger_gap_plan(max_day, floor)
        merged = _ledger_aggs(pool, agent_id, day_lt=gap.day_lt)
        _loki_aggs_into(merged, agent_id, gap.tail_from, None, deadline=deadline)

    cost = _to_agent_cost(merged)
    with _cache_lock:
        _cache[key] = (now_mono + _CACHE_TTL_S, cost)
        if len(_cache) > 4096:  # prune expired on the rare overflow
            expired = [k for k, v in _cache.items() if v[0] <= now_mono]
            for k in expired:
                del _cache[k]
    return cost


def cache_clear() -> None:
    """Test seam: drop the TTL cache."""
    with _cache_lock:
        _cache.clear()


__all__ = ["HALT_EVENT", "agent_cost", "cache_clear", "window_bounds"]
