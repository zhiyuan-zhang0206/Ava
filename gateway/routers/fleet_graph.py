"""Fleet graph endpoint — weighted agent graph for force-directed visualization.

Nodes carry agent identity + status + a recent-work score; edges carry a
dynamic weight that sums per-event recency decay over a time window.

Data sources (task #1197 LGTM cutover):
- `agents_meta` + `agents` (Postgres): node identity, liveness, labels.
- Prometheus (`gateway/prom_metrics.py`): the llm_usage token aggregates —
  retained-window (7d) totals + selected-window scores — from the OTLP-mapped
  counters `ava_llm_usage_in_total` / `ava_llm_usage_out_total`. The retained
  total uses `increase()` so exporter restarts do not reset the reported value.
- Edge events (audit category, spawn/send_message/fork/resurrect): cached raw
  rows from the Loki archive stream (task #1281 — all pre-cutover events)
  stitched with the live stream. The live stream's frozen pre-index-label
  interval is cached separately from its indexed tail.

Successful Prometheus/Loki reads also pass through the gateway-latency
heartbeat guard. Old or missing heartbeat samples retain and cache the fetched
graph, marked separately as telemetry-degraded; only incomplete or fallback
data uses the graph's stale flag.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, LiteralString, NamedTuple

import httpx
from fastapi import APIRouter, Query, Request
from psycopg import errors as pg_errors

from gateway import (
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
from shared.loki_index_labels import ARCHIVE_FLOOR_AT, ARCHIVE_FREEZE_AT, INDEX_LABEL_CUTOVER_AT
from shared.observability import cluster_label
from shared.redis_client import sync_redis

router = APIRouter()

# The fleet view polls every 30s while the underlying data moves slowly
# (retained-window token totals, recency-decayed edge weights). A 60s Redis TTL makes
# alternating polls cache hits, cutting expensive composite reads in half while
# SSE invalidation still carries lifecycle changes promptly. Cache is fail-open:
# a Redis outage degrades to a direct query, never to a 500.
_CACHE_TTL_SECONDS = 60
_LAST_GOOD_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_FROZEN_CACHE_TTL_SECONDS = 24 * 60 * 60
_FROZEN_ARCHIVE_CACHE_KEY = "fleet_graph:frozen:archive:v1"
_FROZEN_LEGACY_CACHE_KEY = "fleet_graph:frozen:legacy:v1"

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
    """Cache a successful graph while reporting heartbeat health separately."""
    try:
        telemetry_stale = telemetry_staleness.check_and_report(timeout_s=3.0)
    except Exception as exc:
        logger.debug("fleet_graph telemetry staleness guard failed open: {}", exc)
        telemetry_stale = False

    response = FleetGraphResponse(
        nodes=nodes,
        edges=edges,
        stale=truncated,
        truncated=truncated,
        telemetry_stale=telemetry_stale,
        snapshot_at=datetime.now(UTC),
    )
    # A truncated graph is the best current view for the next poll, but it is
    # not a complete fallback snapshot. Heartbeat lag is observability health,
    # not a reason to discard an otherwise successful complete snapshot.
    try:
        with sync_redis(decode_responses=True) as redis:
            serialized = response.model_dump_json()
            redis.set(key, serialized, ex=_CACHE_TTL_SECONDS)
            if not truncated:
                redis.set(_last_good_cache_key(key), serialized, ex=_LAST_GOOD_CACHE_TTL_SECONDS)
    except Exception as exc:
        # Fail-open: a cache write failure must not fail the response.
        logger.debug("fleet_graph cache write failed: {}", exc)
    return response


def _read_frozen_json(key: str, *, cache_name: str) -> Any | None:
    """Read one frozen-source payload, treating Redis or JSON errors as misses."""
    try:
        with sync_redis(decode_responses=True) as redis:
            cached = redis.get(key)
        return json.loads(cached) if cached is not None else None
    except Exception as exc:
        logger.debug(
            "fleet_graph frozen {} cache read failed — querying source: {}",
            cache_name,
            exc,
        )
        return None


def _write_frozen_json(key: str, payload: object, *, cache_name: str) -> None:
    """Write one frozen-source payload without making Redis route-critical."""
    try:
        with sync_redis(decode_responses=True) as redis:
            redis.set(key, json.dumps(payload), ex=_FROZEN_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.debug("fleet_graph frozen {} cache write failed: {}", cache_name, exc)


def _edge_row(agent: Any, target: Any, event_name: Any, ts: Any) -> dict[str, Any]:
    """Normalize cached and source edge rows to the Loki query shape."""
    return {
        "agent_id": agent,
        "target_agent_id": target,
        "event_name": event_name,
        "ts": ts,
    }


def _read_legacy_loki_cache() -> list[dict[str, Any]] | None:
    raw = _read_frozen_json(_FROZEN_LEGACY_CACHE_KEY, cache_name="legacy Loki")
    if raw is None:
        return None
    try:
        return [_edge_row(row[0], row[1], row[2], datetime.fromisoformat(row[3])) for row in raw]
    except Exception as exc:
        logger.debug("fleet_graph frozen legacy Loki cache decode failed: {}", exc)
        return None


def _write_legacy_loki_cache(rows: list[dict[str, Any]]) -> None:
    # This historical interval no longer receives normal writes. A collector
    # retry backlog can therefore remain masked until this 24-hour entry expires.
    payload = [
        [row["agent_id"], row["target_agent_id"], row["event_name"], row["ts"].isoformat()]
        for row in rows
    ]
    _write_frozen_json(_FROZEN_LEGACY_CACHE_KEY, payload, cache_name="legacy Loki")


def _query_loki_edge_slice(*, from_: datetime, to: datetime) -> tuple[list[dict[str, Any]], bool]:
    """Query one edge interval with the endpoint's fixed Loki contract."""
    return loki_events.query_events(
        event_names=list(_EDGE_EVENT_NAMES),
        categories=["audit"],
        cluster=cluster_label(),
        from_=from_,
        to=to,
        limit=_LOKI_EDGE_LIMIT,
        direction="forward",
        timeout_s=_TELEMETRY_READ_TIMEOUT_S,
    )


def _fetch_loki_edges(*, now: datetime) -> tuple[list[dict[str, Any]], bool]:
    """Audit rows from cached legacy Loki history plus the live indexed tail.

    Lineage events are all-time (fetch from the archive freeze); message
    events additionally respect the `hours` window, applied per row by the
    caller — the lineage tail must not be clipped by the message window. The
    per-request timeout bounds the expensive tail read before the route
    degrades."""
    legacy_end = min(INDEX_LABEL_CUTOVER_AT, now)
    legacy_rows: list[dict[str, Any]] = []
    legacy_has_more = False
    if legacy_end > ARCHIVE_FREEZE_AT:
        cached_legacy = _read_legacy_loki_cache()
        if cached_legacy is None:
            cached_legacy, legacy_has_more = _query_loki_edge_slice(
                from_=ARCHIVE_FREEZE_AT, to=legacy_end
            )
            _write_legacy_loki_cache(cached_legacy)
        else:
            # The versioned payload contains rows only, so a full cached page
            # conservatively preserves the possibility of truncation.
            legacy_has_more = len(cached_legacy) >= _LOKI_EDGE_LIMIT
        # Loki range endpoints are inclusive. Keep the legacy interval
        # half-open so the separately queried indexed slice owns cutover.
        legacy_rows = [row for row in cached_legacy if row["ts"] < legacy_end]

    indexed_rows: list[dict[str, Any]] = []
    indexed_has_more = False
    indexed_start = max(INDEX_LABEL_CUTOVER_AT, ARCHIVE_FREEZE_AT)
    if indexed_start < now:
        indexed_rows, indexed_has_more = _query_loki_edge_slice(from_=indexed_start, to=now)
    rows = [*legacy_rows, *indexed_rows]
    has_more = legacy_has_more or indexed_has_more
    if has_more:
        logger.warning(
            "fleet_graph Loki edge stream exceeded the {}-row fetch cap — edges truncated",
            _LOKI_EDGE_LIMIT,
        )
    return rows, has_more


def _merge_edge_rows(
    archive_rows: list[dict[str, Any]],
    loki_rows: list[dict[str, Any]],
    *,
    live_ids: set[int] | None,
    win_start: datetime | None,
    now: datetime,
    decay_lambda: float,
) -> list[FleetGraphEdge]:
    """Merge the archive and Loki edge rows per (from, to, event_type).

    The two sides partition the timeline at the freeze boundary (no overlap,
    no gap). Both now carry raw rows, so one loop applies the exact same live
    endpoint, message-window, and per-event weighting semantics."""
    merged: dict[tuple[int, int, str], list[Any]] = {}

    def _absorb(key: tuple[int, int, str], weight: float, count: int, last_seen: datetime) -> None:
        slot = merged.get(key)
        if slot is None:
            merged[key] = [weight, count, last_seen]
        else:
            slot[0] += weight
            slot[1] += count
            slot[2] = max(slot[2], last_seen)

    for rows in (archive_rows, loki_rows):
        for r in rows:
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


def _fetch_archive_edges() -> tuple[list[dict[str, Any]], bool]:
    """Pre-cutover edge rows from the Loki archive stream (task #1281).

    The archive stream holds every pre-cutover audit row; the query is
    bounded to the archive's own span (within Loki's 90d max_query_length)
    and the caller caches the result, so the multi-second whole-archive scan
    runs at most once a day."""
    rows, has_more = loki_events.query_events(
        event_names=list(_EDGE_EVENT_NAMES),
        categories=["audit"],
        from_=ARCHIVE_FLOOR_AT,
        to=ARCHIVE_FREEZE_AT,
        limit=_LOKI_EDGE_LIMIT,
        direction="forward",
        # The whole-archive scan measured ~5.7s on prod; the result is cached
        # for 24h, so a cold-cache fetch gets a generous budget (the live-tail
        # reads keep the tighter 8s).
        archive=True,
        timeout_s=30.0,
    )
    if has_more:
        logger.warning(
            "fleet_graph Loki archive edge stream exceeded the %d-row fetch cap — edges truncated",
            _LOKI_EDGE_LIMIT,
        )
    return rows, has_more


def _read_archive_cache() -> list[dict[str, Any]] | None:
    raw = _read_frozen_json(_FROZEN_ARCHIVE_CACHE_KEY, cache_name="Loki archive")
    if raw is None:
        return None
    try:
        return [
            _edge_row(row[1], row[0], row[2], datetime.fromisoformat(row[3])) for row in raw["rows"]
        ]
    except Exception as exc:
        logger.debug("fleet_graph frozen Loki archive cache decode failed: {}", exc)
        return None


def _write_archive_cache(rows: list[dict[str, Any]]) -> None:
    payload = {
        "rows": [
            [row["target_agent_id"], row["agent_id"], row["event_name"], row["ts"].isoformat()]
            for row in rows
        ],
    }
    _write_frozen_json(_FROZEN_ARCHIVE_CACHE_KEY, payload, cache_name="Loki archive")


class _PgGraphData(NamedTuple):
    """The DB-bound graph phase, kept separate from upstream telemetry work."""

    node_rows: list[tuple[Any, ...]]


def _fetch_pg_graph(
    pool: Any,
    *,
    not_terminated: LiteralString,
) -> _PgGraphData:
    """Fetch nodes under the route's PG budget."""
    with pool.connection() as conn, conn.cursor() as cur:
        # Bound the PG phase below the route deadline so a sync route worker
        # can degrade rather than wait for the pool's normal 60-second limit.
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
    return _PgGraphData(node_rows)


def _fetch_prom_tokens(
    hours: StatsWindowHours | None,
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    """Fetch independent retained and selected-window token aggregates in parallel."""
    # All four counter reads are independent, including the retained and
    # selected-window pairs, so issue them together instead of adding four
    # 8-second waits to the route's sync worker occupancy.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="fleet-prom") as executor:
        futures = {
            "in_retained": executor.submit(
                prom_metrics.sum_by,
                _IN_METRIC,
                "agent_id",
                window=timedelta(days=7),
                timeout_s=_TELEMETRY_READ_TIMEOUT_S,
            ),
            "out_retained": executor.submit(
                prom_metrics.sum_by,
                _OUT_METRIC,
                "agent_id",
                window=timedelta(days=7),
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
            futures["in_retained"].result(),
            futures["out_retained"].result(),
            futures["in_window"].result(),
            futures["out_window"].result(),
        )


def _build_nodes(
    node_rows: list[tuple[Any, ...]],
    *,
    in_retained: dict[str, float] | None = None,
    out_retained: dict[str, float] | None = None,
    in_win: dict[str, float] | None = None,
    out_win: dict[str, float] | None = None,
) -> list[FleetGraphNode]:
    """Build graph nodes, retaining PG identity when metrics are unavailable."""
    in_retained = in_retained or {}
    out_retained = out_retained or {}
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
            total_tokens=round(in_retained.get(str(r[0]), 0.0) + out_retained.get(str(r[0]), 0.0)),
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
    decay_lambda: Annotated[float, Query(ge=0, le=10)] = 0.5,
) -> FleetGraphResponse:
    """Fleet-wide weighted agent graph — nodes (agents) + edges (lineage + messages).

    Nodes carry status, label, a windowed recent-work `node_score`, and
    restart-proof `total_tokens` consumed in the retained window (7d). Edges
    split into two families: lineage
    (spawn/fork/resurrect) is structural and permanent; messages (send_message)
    decay with recency. Terminated agents — and edges touching a terminated
    agent — are excluded by default (user ruling 2026-08-09 #1104: terminated
    agents never appear in the graph, mirroring the sidebar's agent tree). The
    filter ORDER is liveness first: the node set is live-only (`status !=
    'terminated'`, so hibernating/restarting etc. stay), and edges only ever
    connect two live endpoints — a live node whose lineage partner has since
    terminated simply renders without that edge. Raw source rows are filtered
    during the merge; pass `?include_terminated=true` for the full graph.

    `?hours=` (0 = last 5m; 1/6/24/72/168 = hours; omitted = all-time) windows
    both the node score and the edge events. `?decay_lambda=` (range [0, 10],
    default 0.5) is the per-day decay constant for the message edge weight,
    quantized to 2dp before both computation and cache-key construction. Its
    1001 values, two terminated states, and the bounded hour-window choices
    cap the cache-key space at approximately 16k entries. Per-caller rate
    limiting was considered and deferred: this endpoint is auth-gated, and the
    bounded key space leaves no present threat that warrants that infrastructure.

    Node score (windowed, drives node size):
        node_score = SUM(in_total) * 0.1 + SUM(out_total) * 1.0
    over the agent's `llm_usage` counters in the window — read from
    Prometheus (`ava_llm_usage_in_total` / `ava_llm_usage_out_total`,
    windowed via `increase(...)`). `total_tokens` is the sum of the same two
    counters over the retained 7d window, also using `increase(...)` so
    exporter process restarts do not reset it.

    Edge weight:
        lineage (spawn/fork/resurrect): weight = event_count * 2.0 (no time decay,
            always shown — the structural skeleton never fades)
        message (send_message): weight = SUM(EXP(-decay_lambda * days_ago)) * 1.0
            (recency-decayed; dropped below 0.01)
    """
    decay_lambda = round(decay_lambda, 2)

    now = datetime.now(UTC)
    not_terminated: LiteralString = "" if include_terminated else "WHERE a.status != 'terminated'"
    win_start = now - window_delta(hours) if hours is not None else None

    key = _cache_key(include_terminated=include_terminated, hours=hours, decay_lambda=decay_lambda)
    cached = _read_cached_graph(key)
    if cached is not None:
        return cached

    deadline = _monotonic() + _ROUTE_TIMEOUT_S
    try:
        pg_data = _fetch_pg_graph(
            request.app.state.db_pool,
            not_terminated=not_terminated,
        )
    except pg_errors.QueryCanceled:
        # A canceled PG query cannot provide a fresh node set, but a complete
        # prior graph is still strictly more useful than an empty fleet.
        logger.warning("fleet_graph query canceled (statement timeout) — serving stale graph")
        return _stale_graph(key, [])

    node_rows = pg_data.node_rows

    # A phase that crosses the TTFB deadline has missed its budget. Do not
    # reject a merely late successful final assembly: only a completed phase
    # triggers degradation, and degraded results never replace last-good data.
    if _monotonic() > deadline:
        logger.warning("fleet_graph PG phase exceeded route budget — serving stale graph")
        return _stale_graph(key, _build_nodes(node_rows))

    # --- Pre-cutover edges from the Loki archive stream (task #1281) ---
    # The whole-archive scan is slow but served once per day from the 24h
    # Redis cache; a failure degrades with the node set it did have.
    try:
        archive_rows = _read_archive_cache()
        if archive_rows is None:
            archive_rows, _ = _fetch_archive_edges()
            _write_archive_cache(archive_rows)
    except (httpx.HTTPError, loki_query_budget.LokiQueryBudgetError) as exc:
        logger.warning("fleet_graph archive query failed — serving stale graph: {}", exc)
        return _stale_graph(key, _build_nodes(node_rows))

    # --- Token aggregates from Prometheus (the llm_usage counters) ---
    # total_tokens is the restart-proof retained-window sum; node_score is the
    # selected-window weighted score (node size). Both read the OTLP-mapped
    # counters via gateway/prom_metrics; configured windows become PromQL
    # range selectors (increase over [Nh]) instead of SQL fragments.
    try:
        in_retained, out_retained, in_win, out_win = _fetch_prom_tokens(hours)
    except prom_metrics.PromQueryBudgetError as exc:
        logger.warning("fleet_graph Prometheus query budget refused — serving stale graph: {}", exc)
        return _stale_graph(key, _build_nodes(node_rows))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("fleet_graph Prometheus query failed — serving stale graph: {}", exc)
        return _stale_graph(key, _build_nodes(node_rows))

    nodes = _build_nodes(
        node_rows,
        in_retained=in_retained,
        out_retained=out_retained,
        in_win=in_win,
        out_win=out_win,
    )

    if _monotonic() > deadline:
        logger.warning("fleet_graph Prometheus phase exceeded route budget — serving stale graph")
        return _stale_graph(key, nodes)

    # --- Loki side: cached legacy history + live indexed tail ---
    try:
        loki_rows, truncated = _fetch_loki_edges(now=now)
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
        win_start=win_start,
        now=now,
        decay_lambda=decay_lambda,
    )

    return _finalize_graph_response(
        key=key,
        nodes=nodes,
        edges=edges,
        truncated=truncated,
    )
