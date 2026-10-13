"""Fleet graph endpoint — weighted agent graph for force-directed visualization.

Nodes carry agent identity + status + a recent-work score; edges carry a
dynamic weight that sums per-event recency decay over a time window.

Data sources (task #1197 LGTM cutover):
- `agents_meta` + `agents` (Postgres): node identity, liveness, labels.
- Postgres: the llm_usage token sums — retained-window (7d) totals + selected-window
  scores — from `telemetry_events`, the day-grain ledger and the folded
  `agent_model_tokens_total` (`gateway/routers/fleet_tokens.py`), read in the same
  connection as the nodes.
- Edge events (audit category, spawn/send_message/fork/resurrect): aggregated
  in Postgres from `audit_events`, the permanent audit record
  (base/events/reads/audit_rows.py), in the same phase as the nodes.

A successful graph also passes through the gateway-latency heartbeat guard
(`telemetry_staleness`, over `telemetry_events`). An old or missing heartbeat
retains and caches the fetched graph, marked separately as telemetry-degraded.

A failed or timed-out Postgres read is not answered from a cached copy: the
exception propagates to the gateway's unhandled-exception handler (a logged 500).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, LiteralString, NamedTuple

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError
from redis.exceptions import RedisError

from base.config import settings
from base.events.live.bus import EventBus
from base.events.reads import audit_rows
from base.log import logger
from gateway.lgtm.telemetry_staleness import TelemetryStaleness
from gateway.routers.fleet_tokens import AgentTokens, agent_tokens
from gateway.schemas.fleet_graph import FleetGraphEdge, FleetGraphNode, FleetGraphResponse
from gateway.schemas.stats import StatsWindowHours, window_delta

router = APIRouter()

# The fleet view polls every 30s while the underlying data moves slowly
# (retained-window token totals, recency-decayed edge weights). A 60s Redis TTL makes
# alternating polls cache hits, cutting expensive composite reads in half while
# SSE invalidation still carries lifecycle changes promptly. Cache is fail-open:
# a Redis outage degrades to a direct query, never to a 500.
_CACHE_TTL_SECONDS = 60


def cache_key(
    *, include_terminated: bool, hours: StatsWindowHours | None, decay_lambda: float
) -> str:
    return (
        f"fleet_graph:{int(include_terminated)}:"
        f"{int(hours) if hours is not None else 'all'}:{decay_lambda}"
    )


def _read_cached_graph(bus: EventBus, key: str) -> FleetGraphResponse | None:
    """Serve the short-lived poll cache when it exists; fail-open on an unavailable Redis."""
    try:
        with bus.sync_redis(decode_responses=True) as redis:
            cached = redis.get(key)
        if cached is not None:
            return FleetGraphResponse.model_validate_json(cached)
    except (RedisError, OSError, ValidationError) as exc:
        logger.debug("fleet_graph cache read failed — falling back to direct query: {}", exc)
    return None


def _finalize_graph_response(
    pool: Any,
    *,
    bus: EventBus,
    staleness: TelemetryStaleness,
    key: str,
    nodes: list[FleetGraphNode],
    edges: list[FleetGraphEdge],
) -> FleetGraphResponse:
    """Cache a successful graph while reporting heartbeat health separately."""
    try:
        telemetry_stale = staleness.check_and_report(pool)
    except Exception:
        logger.opt(exception=True).warning(
            "fleet_graph telemetry staleness guard raised; reporting telemetry_stale=False"
        )
        telemetry_stale = False

    response = FleetGraphResponse(
        nodes=nodes,
        edges=edges,
        telemetry_stale=telemetry_stale,
        snapshot_at=datetime.now(UTC),
    )
    # Heartbeat lag is observability health, not a reason to discard an
    # otherwise successful complete snapshot.
    try:
        with bus.sync_redis(decode_responses=True) as redis:
            redis.set(key, response.model_dump_json(), ex=_CACHE_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        # Fail-open: a cache write failure must not fail the response.
        logger.debug("fleet_graph cache write failed: {}", exc)
    return response


class _PgGraphData(NamedTuple):
    """The DB-bound graph phase, kept separate from upstream telemetry work."""

    node_rows: list[tuple[Any, ...]]
    edges: list[FleetGraphEdge]
    tokens: dict[int, AgentTokens]


def _edges_from(
    rows: list[tuple[int, int, str, float, int, datetime]],
) -> list[FleetGraphEdge]:
    """Graph edges from the audit aggregates, heaviest first.

    A message edge whose decayed weight has fallen to 0.01 or below is dropped;
    lineage edges are structural and always shown.
    """
    edges = [
        FleetGraphEdge(
            from_agent=target,
            to_agent=agent,
            event_type=name,
            weight=round(weight, 4),
            event_count=count,
            last_seen_at=last_seen.isoformat(),
        )
        for target, agent, name, weight, count, last_seen in rows
        if name != "send_message" or weight > 0.01
    ]
    edges.sort(key=lambda edge: edge.weight, reverse=True)
    return edges


def _fetch_pg_graph(
    pool: Any,
    *,
    not_terminated: LiteralString,
    include_terminated: bool,
    win_start: datetime | None,
    now: datetime,
    decay_lambda: float,
) -> _PgGraphData:
    """Fetch nodes, edges and token sums under the route's PG budget.

    Edges connect two live endpoints unless terminated agents are included; the
    live set is the node set just read.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        # Bound the PG phase so a stalled read fails fast instead of holding a sync
        # route worker for the pool's normal 60-second limit.
        cur.execute("SET LOCAL statement_timeout = '8000'")
        cur.execute(
            # S608: the terminated filter is the only spliced fragment and it
            # is a fixed internal literal (not caller input).
            "SELECT "
            "    a.id, "
            "    t.label, "
            "    a.status, "
            "    a.liveness_state, "
            "    a.born_spawner, "
            "    a.machine "
            "FROM agents_meta a "
            "JOIN agents t ON t.id = a.id " + not_terminated + " ORDER BY a.id"
        )
        node_rows = cur.fetchall()
        live_ids = None if include_terminated else {int(row[0]) for row in node_rows}
        edge_rows = audit_rows.edge_weights(
            conn, live_ids=live_ids, win_start=win_start, now=now, decay_lambda=decay_lambda
        )
        tokens = agent_tokens(conn, now=now, win_start=win_start)
    return _PgGraphData(node_rows, _edges_from(edge_rows), tokens)


def _build_nodes(
    node_rows: list[tuple[Any, ...]], tokens: dict[int, AgentTokens]
) -> list[FleetGraphNode]:
    """Build graph nodes; an agent without token rows scores zero."""
    none = AgentTokens(0.0, 0.0, 0.0, 0.0)
    return [
        FleetGraphNode(
            agent_id=r[0],
            label=r[1],
            status=r[2],
            liveness_state=r[3],
            spawner=r[4],
            machine=r[5],
            total_tokens=round(
                tokens.get(r[0], none).in_retained + tokens.get(r[0], none).out_retained
            ),
            node_score=round(
                tokens.get(r[0], none).in_window * 0.1 + tokens.get(r[0], none).out_window, 2
            ),
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
    # `decay_lambda`'s range stays a protective constant (import-time Query
    # bound; task #3696 exception inventory); the default *decay* is
    # display.fleet_graph_decay_lambda.
    decay_lambda: Annotated[float | None, Query(ge=0, le=10)] = None,
) -> FleetGraphResponse:
    """Fleet-wide weighted agent graph — nodes (agents) + edges (lineage + messages).

    Nodes carry status, label, a windowed recent-work `node_score`, and
    `total_tokens` consumed in the retained window (7d). Edges
    split into two families: lineage
    (spawn/fork/resurrect) is structural and permanent; messages (send_message)
    decay with recency. Terminated agents — and edges touching a terminated
    agent — are excluded by default (user ruling 2026-08-09 #1104: terminated
    agents never appear in the graph, mirroring the sidebar's agent tree). The
    filter ORDER is liveness first: the node set is live-only (`status !=
    'terminated'`), and edges only ever
    connect two live endpoints — a live node whose lineage partner has since
    terminated simply renders without that edge. Raw source rows are filtered
    during the merge; pass `?include_terminated=true` for the full graph.

    `?hours=` (0 = last 5m; 1/6/24/72/168 = hours; omitted = all-time) windows
    both the node score and the edge events. `?decay_lambda=` (range [0, 10];
    omitted = the configured default ``display.fleet_graph_decay_lambda`` -
    0.5 out of the box) is the per-day decay constant for the message edge weight,
    quantized to 2dp before both computation and cache-key construction. Its
    1001 values, two terminated states, and the bounded hour-window choices
    cap the cache-key space at approximately 16k entries. Per-caller rate
    limiting was considered and deferred: this endpoint is auth-gated, and the
    bounded key space leaves no present threat that warrants that infrastructure.

    Node score (windowed, drives node size):
        node_score = SUM(in_total) * 0.1 + SUM(out_total) * 1.0
    over the agent's `llm_usage` rows in the window. `total_tokens` is the sum of
    the same two fields over the retained 7d window. Both are read in parts
    (`gateway/routers/fleet_tokens.py`): raw rows of the newest two days, the
    day-grain ledger before them, and for the all-time score the folded
    `agent_model_tokens_total`, so the read does not scan history.

    Edge weight:
        lineage (spawn/fork/resurrect): weight = event_count * 2.0 (no time decay,
            always shown — the structural skeleton never fades)
        message (send_message): weight = SUM(EXP(-decay_lambda * days_ago)) * 1.0
            (recency-decayed; dropped below 0.01)
    """
    if decay_lambda is None:
        decay_lambda = settings.display.fleet_graph_decay_lambda
    decay_lambda = round(decay_lambda, 2)

    now = datetime.now(UTC)
    not_terminated: LiteralString = "" if include_terminated else "WHERE a.status != 'terminated'"
    win_start = now - window_delta(hours) if hours is not None else None

    key = cache_key(include_terminated=include_terminated, hours=hours, decay_lambda=decay_lambda)
    bus: EventBus = request.app.state.bus
    cached = _read_cached_graph(bus, key)
    if cached is not None:
        return cached

    pg_data = _fetch_pg_graph(
        request.app.state.db_pool,
        not_terminated=not_terminated,
        include_terminated=include_terminated,
        win_start=win_start,
        now=now,
        decay_lambda=decay_lambda,
    )

    nodes = _build_nodes(pg_data.node_rows, pg_data.tokens)

    return _finalize_graph_response(
        request.app.state.db_pool,
        bus=bus,
        staleness=request.app.state.telemetry_staleness,
        key=key,
        nodes=nodes,
        edges=pg_data.edges,
    )
