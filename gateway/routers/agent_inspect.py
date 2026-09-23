"""Independent current-state, statistics, and extension reads for one agent."""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from opentelemetry import metrics
from psycopg import Error as DatabaseError
from psycopg_pool import ConnectionPool, PoolTimeout

from gateway import neighbors
from gateway.routers import _inspect_metrics, _plugin_inspector, _plugin_metrics
from gateway.routers._inspect_cache import InspectCacheFullError, InspectQueryCache
from gateway.routers._inspect_live import db_rows_blocking, notice_blocking, project_heartbeat
from gateway.schemas import (
    AgentInspectLive,
    AgentInspectStatistics,
    HeartbeatLastPause,
    InspectWidgetResult,
    NeighborRow,
    NeighborsResponse,
    PluginMetricResult,
    StatsWindowHours,
)
from gateway.shell_ttls import fallback_expiry
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import ShellInfo
from shared.agents import AgentNotFound
from shared.config import settings

router = APIRouter()
_log = logging.getLogger(__name__)
_shell_probe_failures = metrics.get_meter(__name__).create_counter(
    "ava.inspect.shell_probe.failures", unit="{failure}"
)

_HEARTBEAT_PAUSE_LOOKBACK = timedelta(hours=24)


def _heartbeat_last_pause(pool: ConnectionPool[Any], agent_id: int) -> HeartbeatLastPause | None:
    """Read the newest recent committed pause from the indexed durable trail.

    ``ava.self.pause_heartbeat`` inserts this row in the same transaction as
    the pause deadline. The fixed 24-hour display horizon is unchanged;
    current-state reads never depend on telemetry delivery or Loki health.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_at, duration_s FROM heartbeat_pause_log "
            "WHERE agent_id = %s AND created_at >= %s "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (agent_id, datetime.now(tz=UTC) - _HEARTBEAT_PAUSE_LOOKBACK),
        )
        row = cur.fetchone()
    return HeartbeatLastPause(at=row[0], duration_s=row[1]) if row is not None else None


# Per-op probe deadline for the inspector's shell list. The panel polls every
# 5s; a reachable runner answers a shell_probe in milliseconds, and an
# unreachable one fails the connect within this bound (same budget as the
# roster's status_probe). On failure the inspector shows an empty shell list
# rather than failing the whole panel — a down runner is a liveness problem the
# roster reports, not something the inspector should 503 on.
_SHELL_PROBE_TIMEOUT_S = 3.0


def _shell_ttls_blocking(pool: ConnectionPool, agent_id: int) -> dict[int, datetime]:
    """The agent's TTL deadlines from `agent_shell_ttls` — session_id -> expires_at.

    The table lives in the gateway's own Postgres, so the TTL merge happens
    HERE (the runner probe answers session identity + uptime only; the ops
    server on a split runner has no DB access). A session without a row
    falls back to the 24h cap counted from its launch epoch — legacy
    pre-mandate shells, or sessions created by not-yet-updated runners
    during a rollout (gateway.shell_ttls.fallback_expiry).
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, expires_at FROM agent_shell_ttls WHERE agent_id = %s",
            (agent_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


async def _probe_agent_shells(
    agent_id: int, machine: str, pool: ConnectionPool
) -> tuple[list[ShellInfo], bool]:
    """The agent's live persistent shells, probed on the machine it runs on.

    One uniform path for every machine — the gateway never probes sessions itself.
    `machine` is `agents_meta.machine` (the physical host the agent was spawned
    on; 'unknown' only for legacy rows), resolved through the `machines` table
    to that host's ops server URL and asked via the `shell_probe` op; the
    gateway's own box is just another row (its localhost URL), so a
    single-box deployment dials itself exactly like a split deployment dials a
    runner.

    Each probed shell is enriched with its `agent_shell_ttls` deadline (the
    gateway-owned half of the row; see `_shell_ttls_blocking`) — sessions
    without a row keep `expires_at=None` (no TTL).

    Known RPC failures return an unavailable observation, not a successful
    empty set. Malformed successful responses fail rather than invent data.
    """
    try:
        result = await _cluster_rpc.dispatch_to_machine(
            machine, "shell_probe", {"agent_id": agent_id}, timeout_s=_SHELL_PROBE_TIMEOUT_S
        )
    except (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed) as exc:
        _shell_probe_failures.add(1, {"reason": type(exc).__name__})
        _log.warning(
            "shell observation unavailable agent_id=%s reason=%s", agent_id, type(exc).__name__
        )
        return [], False
    shells = [ShellInfo.model_validate(s) for s in result["shells"]]
    if shells:
        ttls = await asyncio.to_thread(_shell_ttls_blocking, pool, agent_id)
        shells = [
            s.model_copy(
                update={
                    "expires_at": ttls.get(s.id) if s.id in ttls else fallback_expiry(s.created_at)
                }
            )
            for s in shells
        ]
    return shells, True


# SQL work is bounded by database timeouts and at most four leaders. Identical
# in-flight requests share a read; completed values are not kept behind a TTL.
_INSPECT_RESPONSE_TIMEOUT_S = 15.0
_InspectKey = tuple[int, int | None]
_inspect_query_cache = InspectQueryCache[_InspectKey, _inspect_metrics.MetricsSnapshot](
    max_entries=32,
    max_inflight=32,
    max_concurrent_loads=4,
)


async def _inspect_rows_cached_async(
    pool: ConnectionPool[Any],
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    spawned_at: datetime,
) -> _inspect_metrics.MetricsSnapshot:
    key = (agent_id, None if hours is None else int(hours))
    try:
        return await _inspect_query_cache.get_or_load_async(
            key,
            lambda: _inspect_metrics.inspect_snapshot(pool, agent_id, hours, spawned_at=spawned_at),
            ttl_s=0,
            now=time_mod.monotonic,
        )
    except InspectCacheFullError as exc:
        raise HTTPException(status_code=503, detail="inspect query queue is full") from exc


def cache_clear() -> None:
    """Reset request admission between isolated tests."""
    _inspect_query_cache.clear()


@router.get("/api/agents/{agent_id}/inspect/live", response_model=AgentInspectLive)
async def get_agent_inspect_live(agent_id: int, request: Request) -> AgentInspectLive:
    """Cheap current-state half of the inspector panel.

    Reads the current projection, notice, and recent committed heartbeat pause
    from Postgres and probes shells on the owning runner. It performs no log
    queries. Unknown agents return 404; shell probe failures report
    shells_available=False. No part of this response is cached.
    """
    pool = request.app.state.db_pool
    db = await asyncio.to_thread(db_rows_blocking, pool, agent_id)
    notice, shells, last_pause = await asyncio.gather(
        asyncio.to_thread(notice_blocking, pool, agent_id),
        _probe_agent_shells(agent_id, db.machine, pool),
        asyncio.to_thread(_heartbeat_last_pause, pool, agent_id),
    )
    return AgentInspectLive(
        agent_id=agent_id,
        machine=db.machine,
        status=db.status,
        liveness_state=db.liveness_state,
        last_probe_at=db.last_probe_at,
        spawned_at=db.spawned_at,
        started_at=db.started_at,
        shells=shells[0],
        shells_available=shells[1],
        observation=db.observation,
        config_overlay=db.config_overlay,
        preset_name=db.preset_name,
        notice=notice,
        heartbeat=project_heartbeat(
            status=db.status,
            last_active_at=db.last_active_at,
            last_heartbeat_at=db.last_heartbeat_at,
            paused_until=db.paused_until,
            agent_id=agent_id,
            pending_inbound=db.pending_inbound,
            last_pause=last_pause,
        ),
    )


@router.get("/api/agents/{agent_id}/inspect/statistics", response_model=AgentInspectStatistics)
async def get_agent_inspect_statistics(
    agent_id: int,
    request: Request,
    hours: Annotated[StatsWindowHours | None, Query()] = None,
) -> AgentInspectStatistics:
    """Read only the selected agent's window-dependent statistics.

    Current state, notices, runner shells, and heartbeat history are owned by
    ``/inspect/live`` and are never fetched here. Persisted reads share only
    in-flight work and have database and admission deadlines. The requested
    window is preserved; unavailable historical coverage is explicit.
    """
    pool = request.app.state.db_pool
    applied_window_hours = None if hours is None else int(hours)
    try:
        spawned_at = await asyncio.to_thread(_statistics_spawned_at, pool, agent_id)
        aggregates = await asyncio.wait_for(
            _inspect_rows_cached_async(
                pool,
                agent_id,
                hours,
                spawned_at=spawned_at,
            ),
            timeout=_INSPECT_RESPONSE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="inspector history query timed out; retry",
            headers={"Retry-After": "1"},
        ) from exc
    except (DatabaseError, PoolTimeout) as exc:
        raise HTTPException(
            status_code=503,
            detail="inspector metrics database unavailable; retry",
            headers={"Retry-After": "1"},
        ) from exc
    return AgentInspectStatistics(
        agent_id=agent_id,
        window_hours=hours,
        applied_window_hours=applied_window_hours,
        cost=aggregates.cost,
        stats=aggregates.stats,
        tps=aggregates.tps,
        activity=aggregates.activity,
        metadata=aggregates.metadata,
    )


def _statistics_spawned_at(pool: ConnectionPool[Any], agent_id: int) -> datetime:
    """Read the immutable lifecycle origin without loading a live snapshot."""
    with pool.connection(timeout=1.0) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '2s'")
        cur.execute("SELECT spawned_at FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return row[0]


@router.get("/api/agents/{agent_id}/neighbors")
def get_agent_neighbors(
    agent_id: int,
    request: Request,
    # `depth`/`limit` ranges stay protective constants (import-time Query
    # bounds; task #3696 exception inventory); the default *walk* is
    # display.neighbors_default_depth/limit.
    depth: Annotated[int | None, Query(ge=1, le=5)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> NeighborsResponse:
    """The agents most strongly tied to `agent_id`, ranked by recency-weighted
    interaction strength (spawn / fork / resurrect / message, all equal weight),
    plus `ancestors` — the immutable birth chain above `agent_id`, nearest ancestor first.

    Omitted ``depth``/``limit`` return the configured defaults
    (``display.neighbors_default_depth`` / ``display.neighbors_default_limit``
    - 1 / 20 out of the box). `depth=1` returns direct ties only; a higher
    `depth` follows ties outward,
    discounting each extra hop. `ancestors` ignores `depth`/`limit`: it walks
    the immutable born_spawner chain to the top (message ties never form
    ancestors), each row's `depth` = hops up (1 = the direct birth parent).
    Terminated agents are included (each row carries `status`); `limit` caps
    the neighbor count, strongest first. The tie graph reads the unified event
    stream (task #180 LGTM cutover): audit edge events stitch the frozen PG
    `events` archive with the Loki live tail and the walks run in Python
    (gateway/neighbors.py) — the retired `agent_neighbors` SQL function died
    with the frozen table it read.

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
    """
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (agent_id,))
        if cur.fetchone() is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
    if depth is None:
        depth = settings.display.neighbors_default_depth
    if limit is None:
        limit = settings.display.neighbors_default_limit
    ranked, ancestors_ranked, archive_degraded = neighbors.compute(
        root=agent_id,
        max_depth=depth,
        limit=limit,
        db_pool=request.app.state.db_pool,
    )
    ids = list({r[0] for r in ranked} | {r[0] for r in ancestors_ranked})
    label_status: dict[int, tuple[str | None, str]] = {}
    if ids:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.id, t.label, m.status
                FROM agents t
                JOIN agents_meta m ON m.id = t.id
                WHERE t.id = ANY(%s)
                """,
                (ids,),
            )
            label_status = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    neighbors_rows = [
        NeighborRow(
            agent_id=agent,
            label=label_status.get(agent, (None, "terminated"))[0],
            status=label_status.get(agent, (None, "terminated"))[1],
            depth=depth_found,
            score=round(score, 4),
        )
        for agent, depth_found, score in ranked
    ]
    ancestors_rows = [
        NeighborRow(
            agent_id=agent,
            label=label_status.get(agent, (None, "terminated"))[0],
            status=label_status.get(agent, (None, "terminated"))[1],
            depth=depth_found,
            score=round(score, 4),
        )
        for agent, depth_found, score in ancestors_ranked
    ]
    return NeighborsResponse(
        neighbors=neighbors_rows, ancestors=ancestors_rows, degraded=archive_degraded
    )


@router.get("/api/agents/{agent_id}/inspect/metrics")
async def get_agent_plugin_metrics(agent_id: int, request: Request) -> list[PluginMetricResult]:
    """The agent's plugin metrics for the inspector panel — the W13b inspector
    surface of the plugin metric system (see `shared/plugin_metrics.py`).

    Builds the metric registry in process (task #180 PR D — shipped
    plugin `metrics.py` modules + core definitions), keeps the metrics whose
    `output` includes "inspector", renders each template for this agent
    ({{agent_id}} -> ``agent_id = <n>``), re-validates the rendered query,
    substitutes the Grafana time macros with a fixed recent window (24h in 1h
    buckets), and executes each query — LogQL against Loki, SQL read-only
    against the cluster's Postgres.

    Response: one `PluginMetricResult` per registered inspector metric, in
    registration order. `timeseries` / `barchart` metrics carry `series`
    (bucket ts + value, chronological); `stat` metrics carry `value`. A
    metric whose query fails at runtime carries `error` (the others still
    render); registry-level problems are HTTP errors instead — a template
    failing the safety re-validation -> 500 with the reason, {{agent_id}}
    template without an agent id -> 400 (unreachable here — the id is a path
    param). 404 when the agent does not exist. The frontend panel polls this
    every 5s like the parent /inspect. Implementation in
    ``gateway/routers/_plugin_metrics.py``.
    """
    return await asyncio.to_thread(
        _plugin_metrics.metrics_for_agent, request.app.state.db_pool, agent_id
    )


@router.get("/api/agents/{agent_id}/inspect/widgets")
async def get_agent_inspect_widgets(agent_id: int, request: Request) -> list[InspectWidgetResult]:
    """The agent's plugin widgets for the inspector panel — the extension
    surface where enabled plugins embed widgets (see `shared/plugin_inspector.py`;
    registration mirrors the plugin-metric system).

    Builds the widget registry in process (shipped builtin plugins'
    ``inspector.py`` modules imported under their plugin context, restricted
    to the plugins currently enabled), resolves the closed target vocabulary
    for this agent — the open notice and the queue's task-ownership rule —
    and projects each widget, dropping a button whose target did not resolve
    and a widget left without buttons. Unknown agents return 404 like the
    rest of the /inspect family. Implementation in
    ``gateway/routers/_plugin_inspector.py``.
    """
    return await asyncio.to_thread(
        _plugin_inspector.widgets_for_agent, request.app.state.db_pool, agent_id
    )
