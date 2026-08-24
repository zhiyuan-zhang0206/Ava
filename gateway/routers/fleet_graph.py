"""Fleet graph endpoint — weighted agent graph for force-directed visualization.

Nodes carry agent identity + status + a recent-work score; edges carry a
dynamic weight that sums per-event recency decay over a time window.

Data sources (task #1197 LGTM cutover):
- `agents_meta` + `agents` (Postgres): node identity, liveness, labels.
- Prometheus (`gateway/prom_metrics.py`): the llm_usage token aggregates —
  all-time totals + windowed scores — from the OTLP-mapped counters
  `ava_llm_usage_in_total` / `ava_llm_usage_out_total`.
- Edge events (audit category, spawn/send_message/fork/resurrect): the
  frozen PG `events` archive (pre-cutover structural history) stitched with
  Loki (the live tail). Task #1281 imports the archive into Loki, after
  which the PG side collapses and this read becomes Loki-only.

Successful Prometheus/Loki reads also pass through the gateway-latency
heartbeat guard. Old or missing heartbeat samples keep the fetched data but
mark it stale and prevent it from replacing either Redis cache.
"""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, LiteralString, NamedTuple

import httpx
from fastapi import APIRouter, Query, Request
from psycopg import errors as pg_errors

from gateway import (
    events_archive,
    loki_events,
    loki_query_budget,
    prom_metrics,
    telemetry_staleness,
)
from gateway.schemas import (
    FleetGraphEdge,
    FleetGraphNode,
    FleetGraphResponse,
    StatsWindowHours,
    window_delta,
)
from shared.log import logger
from shared.observability import cluster_label
from shared.redis_client import sync_redis

router = APIRouter()

# The fleet view polls every 30s while the underlying data moves slowly
# (all-time token totals, recency-decayed edge weights). A 60s Redis TTL makes
# alternating polls cache hits, cutting expensive composite reads in half while
# SSE invalidation still carries lifecycle changes promptly. Cache is fail-open:
# a Redis outage degrades to a direct query, never to a 500.
_CACHE_TTL_SECONDS = 60
_LAST_GOOD_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# Audit event names that form edges. Lineage (spawn/fork/resurrect) is
# permanent and all-time; messages (send_message) decay with recency.
_EDGE_EVENT_NAMES = ("send_message", "spawn", "fork", "resurrect")

# Loki fetch cap for the edge stream. Audit events are low-volume (a few
# thousand since the cutover); the cap is a guardrail, not an expectation.
_LOKI_EDGE_LIMIT = 50_000
_TELEMETRY_READ_TIMEOUT_S = 8.0
_ROUTE_TIMEOUT_S = 10.0

# The OTLP-mapped llm_usage counters (shared/telemetry_otlp._record_metrics:
# int payload field -> Counter named ava_<event>_<field>, Prometheus appends
# `_total`). The token totals are the sum of the two counters.
_IN_METRIC = "ava_llm_usage_in_total"
_OUT_METRIC = "ava_llm_usage_out_total"


def _monotonic() -> float:
    """Route-budget clock seam, kept local so tests do not alter anyio timing."""
    return time.monotonic()


def _cache_key(
    *, include_terminated: bool, hours: StatsWindowHours | None, decay_lambda: float
) -> str:
    return (
        f"fleet_graph:{int(include_terminated)}:"
        f"{int(hours) if hours is not None else 'all'}:{decay_lambda}"
    )


def _last_good_cache_key(key: str) -> str:
    """Stable full-response fallback corresponding to one poll-cache key."""
    return f"fleet_graph:last_good:{key}"


class _EdgeFilterPlan(NamedTuple):
    """Caller-derived fixed SQL fragments and their safely-bound time values."""

    win_start: datetime | None
    edge_win: LiteralString
    win_params: tuple[datetime, ...]
    edge_live: LiteralString


def _edge_filter_plan(
    *, include_terminated: bool, hours: StatsWindowHours | None, now: datetime
) -> _EdgeFilterPlan:
    """Construct graph-edge filter fragments from validated route parameters."""
    edge_live: LiteralString = (
        ""
        if include_terminated
        else (
            " AND agent_id IN (SELECT id FROM agents_meta WHERE status != 'terminated')"
            " AND target_agent_id IN (SELECT id FROM agents_meta WHERE status != 'terminated')"
        )
    )
    if hours is None:
        return _EdgeFilterPlan(None, "", (), edge_live)

    win_start = now - window_delta(hours)
    return _EdgeFilterPlan(
        win_start,
        " AND (event_name <> 'send_message' OR ts >= %s)",
        (win_start,),
        edge_live,
    )


def _read_graph(key: str, *, cache_name: str) -> FleetGraphResponse | None:
    """Read one graph cache entry; fail-open on an unavailable Redis."""
    try:
        with sync_redis(decode_responses=True) as redis:
            cached = redis.get(key)
        if cached is not None:
            return FleetGraphResponse.model_validate_json(cached)
    except Exception as exc:
        logger.debug(
            "fleet_graph {} read failed — falling back to direct query: {}", cache_name, exc
        )
    return None


def _read_cached_graph(key: str) -> FleetGraphResponse | None:
    """Serve the short-lived poll cache when it exists."""
    return _read_graph(key, cache_name="cache")


def _read_last_good_graph(key: str) -> FleetGraphResponse | None:
    """Return the last successful full graph for this parameter combination."""
    return _read_graph(_last_good_cache_key(key), cache_name="last-good cache")


def _stale_graph(key: str, nodes: list[FleetGraphNode]) -> FleetGraphResponse:
    """Prefer a complete last-good graph; otherwise preserve known nodes.

    A degraded response intentionally bypasses the 60-second cache so the
    next poll retries the upstream read instead of extending a failure.
    """
    last_good = _read_last_good_graph(key)
    if last_good is not None:
        return last_good.model_copy(update={"stale": True, "truncated": False})
    return FleetGraphResponse(nodes=nodes, edges=[], stale=True, truncated=False)


def _finalize_graph_response(
    *,
    key: str,
    nodes: list[FleetGraphNode],
    edges: list[FleetGraphEdge],
    truncated: bool = False,
) -> FleetGraphResponse:
    """Mark heartbeat staleness and cache only a fresh full response."""
    try:
        stale = telemetry_staleness.check_and_report(timeout_s=3.0)
    except Exception as exc:
        logger.debug("fleet_graph telemetry staleness guard failed open: {}", exc)
        stale = False

    response = FleetGraphResponse(nodes=nodes, edges=edges, stale=stale, truncated=truncated)
    if stale:
        return response
    # A truncated-but-fresh graph is still the best current view, so cache it
    # under the fresh-only rule; the next poll will re-read its capped edge tail.
    try:
        with sync_redis(decode_responses=True) as redis:
            serialized = response.model_dump_json()
            redis.set(_last_good_cache_key(key), serialized, ex=_LAST_GOOD_CACHE_TTL_SECONDS)
            redis.set(key, serialized, ex=_CACHE_TTL_SECONDS)
    except Exception as exc:
        # Fail-open: a cache write failure must not fail the response.
        logger.debug("fleet_graph cache write failed: {}", exc)
    return response


def _fetch_loki_edges(
    *, boundary: datetime | None, now: datetime
) -> tuple[list[dict[str, Any]], bool]:
    """Live-tail audit rows from Loki since the archive freeze boundary.

    Lineage events are all-time (fetch from the boundary); message events
    additionally respect the `hours` window, applied per row by the caller —
    the lineage tail must not be clipped by the message window. The per-request
    timeout bounds the expensive tail read before the route degrades."""
    loki_from = boundary if boundary is not None else now - timedelta(days=30)
    rows, has_more = loki_events.query_events(
        event_names=list(_EDGE_EVENT_NAMES),
        categories=["audit"],
        cluster=cluster_label(),
        from_=loki_from,
        to=now,
        limit=_LOKI_EDGE_LIMIT,
        direction="forward",
        timeout_s=_TELEMETRY_READ_TIMEOUT_S,
    )
    if has_more:
        logger.warning(
            "fleet_graph Loki edge stream exceeded the {}-row fetch cap — edges truncated",
            _LOKI_EDGE_LIMIT,
        )
    return rows, has_more


def _merge_edge_rows(
    archive_rows: list[tuple[Any, ...]],
    loki_rows: list[dict[str, Any]],
    *,
    live_ids: set[int] | None,
    win_start: datetime | None,
    now: datetime,
    decay_lambda: float,
) -> list[FleetGraphEdge]:
    """Merge the archive and Loki edge rows per (from, to, event_type).

    The two sides partition the timeline at the freeze boundary (no overlap,
    no gap), so a plain add keeps every weight exact: lineage adds 2.0 per
    event, messages add the same EXP decay each row contributed on the SQL
    side. Loki rows touching a terminated endpoint are dropped here (the
    archive side filters them in SQL); message rows older than `win_start`
    (the `hours` window) are dropped per row so the lineage tail is never
    clipped by the message window."""
    merged: dict[tuple[int, int, str], list[Any]] = {}

    def _absorb(key: tuple[int, int, str], weight: float, count: int, last_seen: datetime) -> None:
        slot = merged.get(key)
        if slot is None:
            merged[key] = [weight, count, last_seen]
        else:
            slot[0] += weight
            slot[1] += count
            slot[2] = max(slot[2], last_seen)

    for r in archive_rows:
        _absorb((int(r[0]), int(r[1]), str(r[2])), float(r[3]), int(r[4]), r[5])

    for r in loki_rows:
        target = r.get("target_agent_id")
        agent = r.get("agent_id")
        name = r.get("event_name")
        if target is None or agent is None or name is None:
            continue
        if live_ids is not None and (int(target) not in live_ids or int(agent) not in live_ids):
            continue
        if name == "send_message":
            if win_start is not None and r["ts"] < win_start:
                continue
            weight = math.exp(-decay_lambda * (now - r["ts"]).total_seconds() / 86400.0)
        else:
            weight = 2.0
        _absorb((int(target), int(agent), str(name)), weight, 1, r["ts"])

    edges = [
        FleetGraphEdge(
            from_agent=target,
            to_agent=agent,
            event_type=name,
            weight=round(slot[0], 4),
            event_count=slot[1],
            last_seen_at=slot[2].isoformat(),
        )
        for (target, agent, name), slot in merged.items()
        if name != "send_message" or slot[0] > 0.01
    ]
    edges.sort(key=lambda e: e.weight, reverse=True)
    return edges


def _archive_boundary(cur: Any) -> datetime | None:
    """The frozen PG `events` archive's freeze point — its newest row's ts.

    Rows older than the boundary come from the archive, rows at/after it from
    Loki (task #1280 interim; task #1281 imports the archive into Loki, after
    which the archive read collapses to Loki-only)."""
    return events_archive.load_frozen_boundary(cur)


def _fetch_archive_edges(
    cur: Any,
    *,
    decay_lambda: float,
    now: datetime,
    boundary: datetime | None,
    edge_live: LiteralString,
    edge_win: LiteralString,
    win_params: tuple[datetime, ...],
) -> list[tuple[Any, ...]]:
    """Pre-cutover edge rows from the frozen PG archive, grouped per
    (from, to, event_type) with the same weight semantics as the Loki side
    (lineage COUNT*2.0 permanent; messages EXP-decayed). The decay reference
    is `now` (the same instant the Loki side uses) so the two sides sum
    without drift. S608 — only the fixed window LiteralString fragments are
    spliced; every value binds via %s."""
    edge_params: tuple[float | datetime | int | None, ...] = (
        decay_lambda,
        now,
        boundary,
        *win_params,
        decay_lambda,
        now,
    )
    cur.execute(
        "SELECT "  # noqa: S608
        "    target_agent_id, "
        "    agent_id, "
        "    event_name, "
        "    CASE WHEN event_name = 'send_message' "
        "        THEN SUM(EXP(-%s * EXTRACT(EPOCH FROM (%s - ts)) / 86400.0)) * 1.0 "
        "        ELSE COUNT(*) * 2.0 "
        "    END AS weight, "
        "    COUNT(*) AS event_count, "
        "    MAX(ts) AS last_seen_at "
        "FROM events "
        "WHERE category = 'audit' "
        "  AND event_name IN ('send_message', 'spawn', 'fork', 'resurrect') "
        "  AND target_agent_id IS NOT NULL "
        "  AND agent_id IS NOT NULL "
        "  AND ts < %s" + edge_live + edge_win + " "
        "GROUP BY target_agent_id, agent_id, event_name "
        "HAVING event_name <> 'send_message' "
        "    OR SUM(EXP(-%s * EXTRACT(EPOCH FROM (%s - ts)) / 86400.0)) * 1.0 > 0.01 "
        "ORDER BY weight DESC",
        edge_params,
    )
    return list(cur.fetchall())


class _PgGraphData(NamedTuple):
    """The DB-bound graph phase, kept separate from upstream telemetry work."""

    node_rows: list[tuple[Any, ...]]
    boundary: datetime | None
    archive_rows: list[tuple[Any, ...]]


def _fetch_pg_graph(
    pool: Any,
    *,
    not_terminated: LiteralString,
    edge_live: LiteralString,
    edge_win: LiteralString,
    win_params: tuple[datetime, ...],
    decay_lambda: float,
    now: datetime,
) -> _PgGraphData:
    """Fetch nodes and frozen archive edges under the route's PG budget."""
    with pool.connection() as conn, conn.cursor() as cur:
        # The graph's archive read can scan a large frozen table. Bound the PG
        # phase below the route deadline so a sync route worker can degrade
        # rather than wait for the pool's normal 60-second limit.
        cur.execute("SET LOCAL statement_timeout = '8000'")
        cur.execute(
            # S608: the terminated filter is the only spliced fragment and it
            # is a fixed internal literal (not caller input).
            "SELECT "
            "    a.id, "
            "    t.label, "
            "    a.status, "
            "    a.liveness_state, "
            "    a.spawner, "
            "    a.machine "
            "FROM agents_meta a "
            "JOIN agents t ON t.id = a.id " + not_terminated + " ORDER BY a.id"
        )
        node_rows = cur.fetchall()
        boundary = _archive_boundary(cur)
        archive_rows = _fetch_archive_edges(
            cur,
            decay_lambda=decay_lambda,
            now=now,
            boundary=boundary,
            edge_live=edge_live,
            edge_win=edge_win,
            win_params=win_params,
        )
    return _PgGraphData(node_rows, boundary, archive_rows)


def _fetch_prom_tokens(
    hours: StatsWindowHours | None,
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    """Fetch independent all-time and selected-window token counters in parallel."""
    # All four counter reads are independent, including the all-time and
    # selected-window pairs, so issue them together instead of adding four
    # 8-second waits to the route's sync worker occupancy.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="fleet-prom") as executor:
        futures = {
            "in_all": executor.submit(
                prom_metrics.sum_by,
                _IN_METRIC,
                "agent_id",
                timeout_s=_TELEMETRY_READ_TIMEOUT_S,
            ),
            "out_all": executor.submit(
                prom_metrics.sum_by,
                _OUT_METRIC,
                "agent_id",
                timeout_s=_TELEMETRY_READ_TIMEOUT_S,
            ),
            "in_window": executor.submit(
                prom_metrics.sum_by,
                _IN_METRIC,
                "agent_id",
                window=window_delta(hours) if hours is not None else None,
                timeout_s=_TELEMETRY_READ_TIMEOUT_S,
            ),
            "out_window": executor.submit(
                prom_metrics.sum_by,
                _OUT_METRIC,
                "agent_id",
                window=window_delta(hours) if hours is not None else None,
                timeout_s=_TELEMETRY_READ_TIMEOUT_S,
            ),
        }
        return (
            futures["in_all"].result(),
            futures["out_all"].result(),
            futures["in_window"].result(),
            futures["out_window"].result(),
        )


def _build_nodes(
    node_rows: list[tuple[Any, ...]],
    *,
    in_all: dict[str, float] | None = None,
    out_all: dict[str, float] | None = None,
    in_win: dict[str, float] | None = None,
    out_win: dict[str, float] | None = None,
) -> list[FleetGraphNode]:
    """Build graph nodes, retaining PG identity when metrics are unavailable."""
    in_all = in_all or {}
    out_all = out_all or {}
    in_win = in_win or {}
    out_win = out_win or {}
    return [
        FleetGraphNode(
            agent_id=r[0],
            label=r[1],
            status=r[2],
            liveness_state=r[3],
            spawner=r[4],
            machine=r[5],
            total_tokens=round(in_all.get(str(r[0]), 0.0) + out_all.get(str(r[0]), 0.0)),
            node_score=round(in_win.get(str(r[0]), 0.0) * 0.1 + out_win.get(str(r[0]), 0.0), 2),
        )
        for r in node_rows
    ]


@router.get("/api/fleet/graph")
def get_fleet_graph(
    request: Request,
    include_terminated: Annotated[  # noqa: FBT002
        bool,
        Query(description="Include terminated agents"),
    ] = False,
    hours: Annotated[StatsWindowHours | None, Query()] = None,
    decay_lambda: Annotated[float, Query(ge=0)] = 0.5,
) -> FleetGraphResponse:
    """Fleet-wide weighted agent graph — nodes (agents) + edges (lineage + messages).

    Nodes carry status, label, a windowed recent-work `node_score`, and the
    all-time `total_tokens`. Edges split into two families: lineage
    (spawn/fork/resurrect) is structural and permanent; messages (send_message)
    decay with recency. Terminated agents — and edges touching a terminated
    agent — are excluded by default (user ruling 2026-08-09 #1104: terminated
    agents never appear in the graph, mirroring the sidebar's agent tree). The
    filter ORDER is liveness first: the node set is live-only (`status !=
    'terminated'`, so hibernating/restarting etc. stay), and edges only ever
    connect two live endpoints — a live node whose lineage partner has since
    terminated simply renders without that edge. Filtering at the SQL layer
    saves ~90% of the payload; pass `?include_terminated=true` for the full
    archive.

    `?hours=` (0 = last 5m; 1/6/24/72/168 = hours; omitted = all-time) windows
    both the node score and the edge events. `?decay_lambda=` (>= 0, default
    0.5) is the per-day decay constant for the message edge weight.

    Node score (windowed, drives node size):
        node_score = SUM(in_total) * 0.1 + SUM(out_total) * 1.0
    over the agent's `llm_usage` counters in the window — read from
    Prometheus (`ava_llm_usage_in_total` / `ava_llm_usage_out_total`,
    windowed via `increase(...)`). `total_tokens` is the all-time sum of
    the same two counters (cumulative since the exporting process started).

    Edge weight:
        lineage (spawn/fork/resurrect): weight = event_count * 2.0 (no time decay,
            always shown — the structural skeleton never fades)
        message (send_message): weight = SUM(EXP(-decay_lambda * days_ago)) * 1.0
            (recency-decayed; dropped below 0.01)
    """
    # Windowed-filter fragment spliced into the edge SQL below. Kept as a literal
    # (not an f-string) so the composed query stays a LiteralString for psycopg.
    # None => all-time (no filter, no param). The node token window is applied in
    # PromQL (increase over the selected duration) instead — see the Prometheus block below.
    now = datetime.now(UTC)
    not_terminated: LiteralString = "" if include_terminated else "WHERE a.status != 'terminated'"
    # Same live-frontier rule for edges: an edge touching a terminated agent can
    # never be drawn (its endpoint is not in the node set), so filtering it here
    # shrinks the payload (24h window: ~2436 -> ~150 edges) with no visual change.
    edge_filters = _edge_filter_plan(include_terminated=include_terminated, hours=hours, now=now)

    key = _cache_key(include_terminated=include_terminated, hours=hours, decay_lambda=decay_lambda)
    cached = _read_cached_graph(key)
    if cached is not None:
        return cached

    deadline = _monotonic() + _ROUTE_TIMEOUT_S
    try:
        pg_data = _fetch_pg_graph(
            request.app.state.db_pool,
            not_terminated=not_terminated,
            edge_live=edge_filters.edge_live,
            edge_win=edge_filters.edge_win,
            win_params=edge_filters.win_params,
            decay_lambda=decay_lambda,
            now=now,
        )
    except pg_errors.QueryCanceled:
        # A canceled PG query cannot provide a fresh node set, but a complete
        # prior graph is still strictly more useful than an empty fleet.
        logger.warning("fleet_graph query canceled (statement timeout) — serving stale graph")
        return _stale_graph(key, [])

    node_rows = pg_data.node_rows
    boundary = pg_data.boundary
    archive_rows = pg_data.archive_rows

    # A phase that crosses the TTFB deadline has missed its budget. Do not
    # reject a merely late successful final assembly: only a completed phase
    # triggers degradation, and degraded results never replace last-good data.
    if _monotonic() > deadline:
        logger.warning("fleet_graph PG phase exceeded route budget — serving stale graph")
        return _stale_graph(key, _build_nodes(node_rows))

    # --- Token aggregates from Prometheus (the llm_usage counters) ---
    # total_tokens is the all-time counter sum (tooltip); node_score is the
    # windowed weighted score (node size). Both read the OTLP-mapped counters
    # via gateway/prom_metrics; the `hours` window becomes a PromQL range
    # selector (increase over [Nh]) instead of a SQL fragment.
    try:
        in_all, out_all, in_win, out_win = _fetch_prom_tokens(hours)
    except prom_metrics.PromQueryBudgetError as exc:
        logger.warning("fleet_graph Prometheus query budget refused — serving stale graph: {}", exc)
        return _stale_graph(key, _build_nodes(node_rows))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("fleet_graph Prometheus query failed — serving stale graph: {}", exc)
        return _stale_graph(key, _build_nodes(node_rows))

    nodes = _build_nodes(
        node_rows,
        in_all=in_all,
        out_all=out_all,
        in_win=in_win,
        out_win=out_win,
    )

    if _monotonic() > deadline:
        logger.warning("fleet_graph Prometheus phase exceeded route budget — serving stale graph")
        return _stale_graph(key, nodes)

    # --- Loki side: the live tail of the audit stream ---
    try:
        loki_rows, truncated = _fetch_loki_edges(boundary=boundary, now=now)
    except (httpx.HTTPError, loki_query_budget.LokiQueryBudgetError) as exc:
        logger.warning("fleet_graph Loki query failed — serving stale graph: {}", exc)
        return _stale_graph(key, nodes)

    if _monotonic() > deadline:
        logger.warning("fleet_graph Loki phase exceeded route budget — serving stale graph")
        return _stale_graph(key, nodes)

    live_ids: set[int] | None = None
    if not include_terminated:
        live_ids = {int(r[0]) for r in node_rows}
    edges = _merge_edge_rows(
        archive_rows,
        loki_rows,
        live_ids=live_ids,
        win_start=edge_filters.win_start,
        now=now,
        decay_lambda=decay_lambda,
    )

    return _finalize_graph_response(
        key=key,
        nodes=nodes,
        edges=edges,
        truncated=truncated,
    )
