"""Per-agent cost aggregation — the archive + Loki stitch (task #1273).

The inspector's cost/token section reads two stores on one timeline: the
frozen PG `events` archive (pre-LGTM history) and Loki (the live tail, born
at the cutover, ~7d retention). `_agent_cost` splits the request window at
the archive's freeze point, aggregates each side per model, and merges —
restoring the whole-life cumulative the Loki-only read had silently lost.
Cost never re-prices against the current catalog: rows carry their
usage-time price snapshot (`cost_usd`, written by agent/observe.py), summed
directly; pre-snapshot legacy rows price at read time via `cost_usd` (the
retired-model ledger included) and count as `estimated_calls`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, TypedDict, cast

from psycopg_pool import ConnectionPool

from gateway import loki_events
from gateway.schemas import AgentCost, StatsWindowHours
from shared.events.contract import LLM_USAGE_KEYS
from shared.lm.pricing import cost_usd

# The compact-halt event name (payload key `body` mentions "compact") —
# mirrors `HALT_KEYS` in shared/events/contract.py.
HALT_EVENT = "halt"


class _AggCommon(TypedDict, total=False):
    """Shared filter kwargs for the loki_events aggregate calls — a TypedDict so
    `**common` unpacks with exact types under pyright strict."""

    event_names: list[str]
    categories: list[str]
    agent_id: int
    from_: datetime | None
    to: datetime | None


# "Whole life" for Loki queries. The old PG reads had no `ts >=` clause at
# all; Loki needs an explicit bound, and it rejects windows older than its
# `max_query_length` guardrail (default 30d1h) with a 400 — a far-past
# sentinel would 500 every whole-life request (the inspector 500 of
# 2026-08-12). Data only goes back to the OTLP cutover anyway, so now - 30d
# is the honest equivalent (spawned_at would wrongly cut off any pre-spawn
# test row and is not a real bound — events cannot exist before the agent
# spawns).
_LOKI_MAX_WINDOW = timedelta(days=30)


def _whole_life_start() -> datetime:
    """Lower bound for whole-life Loki queries: now - 30d.

    A call, not a module constant: the bound tracks `now`, and a constant
    would be frozen at import time and drift stale.
    """
    return datetime.now(tz=UTC) - _LOKI_MAX_WINDOW


def _from_bound(
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
) -> tuple[datetime | None, datetime | None]:
    """Resolve the window lower bound for the event-history aggregates.

    Returns (from_, window_start): the concrete lower bound for the Loki
    queries, and the same instant for the alive-time clip. `since_compact`
    wins: the agent's latest compact-halt ts (None = never compacted ->
    whole life). Otherwise `hours` -> now - N h; neither -> whole life.
    Whole life uses `_whole_life_start()` (now - 30d) as the explicit bound
    — Loki's `max_query_length` guardrail rejects anything older, and the
    data only reaches back to the OTLP cutover anyway."""
    if since_compact:
        compact_ts = _compact_ts(agent_id)
        if compact_ts is None:
            return _whole_life_start(), None
        return compact_ts, compact_ts
    if hours is not None:
        window_from = datetime.now(tz=UTC) - timedelta(hours=int(hours))
        return window_from, window_from
    return _whole_life_start(), None


def _compact_ts(agent_id: int) -> datetime | None:
    """The agent's latest `halt` event whose body mentions compact (the
    system_halt emitted when its history is compacted) — the since-compact
    window lower bound; None when the agent never compacted."""
    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=[HALT_EVENT],
        grep="compact",
        from_=_whole_life_start(),
        limit=1,
    )
    return rows[0]["ts"] if rows else None


class _ModelCostAgg(NamedTuple):
    """Per-model cost/token aggregates from one side of the read (archive or
    Loki), before the two sides are merged. `cost` is the sum of the rows'
    stored usage-time price snapshots; `costed_calls` counts the rows that
    carried one. Rows without a snapshot (legacy rows / unpriced calls) are
    priced at read time by the caller via `cost_usd` — which consults the
    retired-model ledger, so a model removed from the registry keeps its
    historical price."""

    calls: int
    costed_calls: int
    tin: int
    tout: int
    tcached: int
    treason: int
    legacy_tin: int
    legacy_tout: int
    legacy_tcached: int
    cost: float


class _CostResult(NamedTuple):
    """`_agent_cost`'s outcome: the merged AgentCost plus the Loki-side token
    totals. The TPS section's denominator (`turn_end.duration_seconds`) is
    Loki-only (the archive read was never migrated for turn stats), so its
    numerator must be the Loki-window tokens — pairing all-time tokens with a
    Loki-only denominator would inflate the rate ~15x (task #1273)."""

    cost: AgentCost
    loki_tokens_out: int


def _archive_boundary(pool: ConnectionPool[Any]) -> datetime | None:
    """The PG `events` archive's freeze point — its newest row's ts.

    The Postgres events copy is a read-only archive since the LGTM cutover
    (task #1197 close-C): nothing writes it, and its rows predate Loki (whose
    storage was born at the cutover and keeps only ~7d). Cost/token history
    must therefore be stitched: rows older than the boundary come from the
    archive, rows at/after it from Loki. The boundary is the archive's own
    max(ts), so the two sides partition the timeline with no gap and no
    double count (the cutover overlap [loki epoch, archive max] is served by
    the archive). None = empty archive (fresh cluster) -> Loki-only reads,
    the pre-cutover behavior.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(ts) FROM events")
        row = cur.fetchone()
    return row[0] if row is not None else None


def _split_cost_window(
    from_: datetime | None, window_start: datetime | None, boundary: datetime | None
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """Partition one request window into (archive_from, archive_to, loki_from).

    `from_` is the Loki-query lower bound (`_from_bound`: hours/since_compact,
    or now - 30d for whole-life — the Loki max_query_length guardrail);
    `window_start` is the same instant but None for whole-life. The archive has
    no Loki-style guardrail, so whole-life reads it UNBOUNDED below; the Loki
    side then covers [boundary, now] only. With no archive (boundary None) the
    whole window goes to Loki unchanged. Returns archive bounds (None,None =
    no archive rows in this window) and the Loki lower bound.
    """
    if boundary is None:
        return None, None, from_
    if window_start is None:  # whole life: full archive + Loki since the cutover
        return None, boundary, boundary
    if window_start < boundary:
        return window_start, boundary, boundary
    return None, None, window_start  # window fully inside Loki's era


_ARCHIVE_COST_SQL = f"""SELECT COALESCE({LLM_USAGE_KEYS["model"]}, '') AS model,
  count(*) AS calls,
  count({LLM_USAGE_KEYS["cost_usd"]}) AS costed_calls,
  sum(coalesce(({LLM_USAGE_KEYS["in_total"]})::bigint, 0)) AS tin,
  sum(coalesce(({LLM_USAGE_KEYS["out_total"]})::bigint, 0)) AS tout,
  sum(coalesce(({LLM_USAGE_KEYS["cache_read"]})::bigint, 0)) AS tcached,
  sum(coalesce(({LLM_USAGE_KEYS["reasoning"]})::bigint, 0)) AS treason,
  coalesce(sum(({LLM_USAGE_KEYS["in_total"]})::bigint)
      FILTER (WHERE {LLM_USAGE_KEYS["cost_usd"]} IS NULL), 0) AS legacy_tin,
  coalesce(sum(({LLM_USAGE_KEYS["out_total"]})::bigint)
      FILTER (WHERE {LLM_USAGE_KEYS["cost_usd"]} IS NULL), 0) AS legacy_tout,
  coalesce(sum(({LLM_USAGE_KEYS["cache_read"]})::bigint)
      FILTER (WHERE {LLM_USAGE_KEYS["cost_usd"]} IS NULL), 0) AS legacy_tcached,
  sum(coalesce(({LLM_USAGE_KEYS["cost_usd"]})::float8, 0)) AS cost
FROM events
WHERE event_name = 'llm_usage' AND category IN ('telemetry', 'log') AND agent_id = %s
  AND ts >= %s AND ts < %s
GROUP BY 1"""  # noqa: S608 — interpolates only registry-derived SQL key fragments (shared/events/contract.py), never request data


def _archive_model_aggs(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> dict[str, _ModelCostAgg]:
    """Per-model llm_usage aggregates from the PG events archive (the frozen
    pre-LGTM history). One grouped scan; snapshot cost sums directly, legacy
    rows (no cost_usd — unpriced at write time, or an archive the backfill
    migration has not touched) keep their token sums separate so the caller
    can price them at read time without double counting."""
    if from_ is None and to is None:
        return {}
    out: dict[str, _ModelCostAgg] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_ARCHIVE_COST_SQL, (agent_id, from_ or datetime.min.replace(tzinfo=UTC), to))
        for row in cur.fetchall():
            out[row[0]] = _ModelCostAgg(
                calls=int(row[1]),
                costed_calls=int(row[2]),
                tin=int(row[3]),
                tout=int(row[4]),
                tcached=int(row[5]),
                treason=int(row[6]),
                legacy_tin=int(row[7]),
                legacy_tout=int(row[8]),
                legacy_tcached=int(row[9]),
                cost=float(row[10]),
            )
    return out


def _loki_model_aggs(
    agent_id: int, from_: datetime | None, to: datetime | None
) -> tuple[dict[str, _ModelCostAgg], int]:
    """Per-model llm_usage aggregates from Loki, mirroring `_archive_model_aggs`.

    Rows written since the price-snapshot change carry `cost_usd` (summed
    directly, `costed_calls` counted via the `cost_usd!=""` filter); legacy
    rows without it are isolated by the `cost_usd=""` filter so the caller
    prices exactly those tokens at read time. Returns (aggs, tokens_out) with
    tokens_out the Loki-side output tokens — the TPS numerator that pairs
    with the Loki-only turn-end denominator."""
    common: _AggCommon = {
        "event_names": ["llm_usage"],
        "categories": ["telemetry", "log"],
        "agent_id": agent_id,
        "from_": from_,
        "to": to,
    }
    legacy = cast(_AggCommon, {**common, "attribute_filters": {"cost_usd": ""}})
    by_model = {
        key: dict(loki_events.attribute_aggregate(field=key, agg="sum", group_by="model", **common))
        for key in ("in_total", "out_total", "cache_read", "reasoning")
    }
    calls_by_model = dict(
        loki_events.attribute_aggregate(field="in_total", agg="count", group_by="model", **common)
    )
    costed_calls_by_model = dict(
        loki_events.attribute_aggregate(
            field="in_total",
            agg="count",
            group_by="model",
            attribute_filters={"cost_usd": "!="},
            **common,
        )
    )
    cost_by_model = dict(
        loki_events.attribute_aggregate(field="cost_usd", agg="sum", group_by="model", **common)
    )
    legacy_by_model = {
        key: dict(loki_events.attribute_aggregate(field=key, agg="sum", group_by="model", **legacy))
        for key in ("in_total", "out_total", "cache_read")
    }
    aggs: dict[str, _ModelCostAgg] = {}
    for model in by_model["in_total"]:
        g_in = dict(by_model["in_total"]).get(model, 0.0)
        g_out = dict(by_model["out_total"]).get(model, 0.0)
        g_cached = dict(by_model["cache_read"]).get(model, 0.0)
        g_reason = dict(by_model["reasoning"]).get(model, 0.0)
        aggs[model] = _ModelCostAgg(
            calls=int(calls_by_model.get(model, 0)),
            costed_calls=int(costed_calls_by_model.get(model, 0)),
            tin=int(g_in),
            tout=int(g_out),
            tcached=int(g_cached),
            treason=int(g_reason),
            legacy_tin=int(legacy_by_model["in_total"].get(model, 0.0)),
            legacy_tout=int(legacy_by_model["out_total"].get(model, 0.0)),
            legacy_tcached=int(legacy_by_model["cache_read"].get(model, 0.0)),
            cost=float(cost_by_model.get(model, 0.0)),
        )
    return aggs, int(sum(a.tout for a in aggs.values()))


def _merge_cost_aggs(aggs: dict[str, _ModelCostAgg]) -> AgentCost:
    """Reduce per-model aggregates to the AgentCost response.

    Cost = stored snapshots + read-time pricing of exactly the legacy rows
    (rows without a snapshot). Legacy rows price via `cost_usd` (the
    retired-model ledger included), so a model removed from the registry
    keeps its historical price; legacy rows of a model with no known price
    count as `unpriced_calls` (cost 0), and rows priced at read time count
    as `estimated_calls` — the price-change immunity applies to snapshot
    rows, and the estimate marker keeps the transition honest."""
    tin = tout = tcached = treason = calls = unpriced = estimated = 0
    total_cost = 0.0
    for model, a in aggs.items():
        tin += a.tin
        tout += a.tout
        tcached += a.tcached
        treason += a.treason
        calls += a.calls
        total_cost += a.cost
        legacy_calls = a.calls - a.costed_calls
        if legacy_calls <= 0:
            continue
        priced = cost_usd(model, a.legacy_tin, a.legacy_tout, a.legacy_tcached)
        if priced is None:
            unpriced += legacy_calls
        else:
            total_cost += priced
            estimated += legacy_calls
    cache_hit_pct = round(tcached / tin * 100, 2) if tin else 0.0
    return AgentCost(
        cost_usd=round(total_cost, 4),
        unpriced_calls=unpriced,
        estimated_calls=estimated,
        llm_calls=calls,
        tokens_in=tin,
        tokens_out=tout,
        tokens_cached=tcached,
        tokens_reasoning=treason,
        cache_hit_pct=cache_hit_pct,
    )


def _agent_cost(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, window_start: datetime | None
) -> _CostResult:
    """LLM cost + token totals for one agent, over the window.

    Two stores, one timeline (task #1273): the frozen PG `events` archive
    holds the pre-LGTM history, Loki the live tail. The request window is
    split at the archive's freeze point (`_archive_boundary`), each side
    aggregates per model, and the sides merge per model — restoring the
    all-time cumulative the Loki-only read had silently lost (405: ~$14.5
    history shown as $0.74 from Loki's ~2d of data). Cost never re-prices
    against the current catalog: rows carry their usage-time price snapshot
    (`cost_usd`), summed directly; pre-snapshot legacy rows price at read
    time via `cost_usd` (retired-model ledger included) and count as
    `estimated_calls`. An unpriced model contributes 0 cost and its calls
    land in `unpriced_calls`."""
    boundary = _archive_boundary(pool)
    pg_from, pg_to, loki_from = _split_cost_window(from_, window_start, boundary)
    merged: dict[str, _ModelCostAgg] = {}
    loki_tokens_out = 0
    if pg_from is not None or pg_to is not None:
        for model, agg in _archive_model_aggs(pool, agent_id, pg_from, pg_to).items():
            merged[model] = agg
    if loki_from is not None:
        loki_aggs, loki_tokens_out = _loki_model_aggs(agent_id, loki_from, None)
        for model, agg in loki_aggs.items():
            if model in merged:
                a = merged[model]
                merged[model] = _ModelCostAgg(
                    calls=a.calls + agg.calls,
                    costed_calls=a.costed_calls + agg.costed_calls,
                    tin=a.tin + agg.tin,
                    tout=a.tout + agg.tout,
                    tcached=a.tcached + agg.tcached,
                    treason=a.treason + agg.treason,
                    legacy_tin=a.legacy_tin + agg.legacy_tin,
                    legacy_tout=a.legacy_tout + agg.legacy_tout,
                    legacy_tcached=a.legacy_tcached + agg.legacy_tcached,
                    cost=a.cost + agg.cost,
                )
            else:
                merged[model] = agg
    return _CostResult(cost=_merge_cost_aggs(merged), loki_tokens_out=loki_tokens_out)
