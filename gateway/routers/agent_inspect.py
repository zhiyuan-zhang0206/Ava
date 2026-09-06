"""Per-agent inspector panel — GET /api/agents/{id}/inspect."""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, NamedTuple

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from opentelemetry import metrics
from psycopg_pool import ConnectionPool

from gateway import loki_events, loki_query_budget, neighbors
from gateway.routers import _agent_cost, _inspect_stats, _plugin_metrics
from gateway.routers._agent_cost import _query_timeout, window_bounds
from gateway.routers._backend_failure import raise_backend_unavailable
from gateway.routers._inspect_cache import InspectCacheFullError, InspectQueryCache
from gateway.routers._inspect_live import db_rows_blocking, notice_blocking, project_heartbeat
from gateway.schemas import (
    AgentActivity,
    AgentInspect,
    AgentInspectLive,
    AgentTps,
    HeartbeatLastPause,
    NeighborRow,
    NeighborsResponse,
    PluginMetricResult,
    StatsWindowHours,
    applied_window,
)
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import ShellInfo
from shared.agents import AgentNotFound

router = APIRouter()
_log = logging.getLogger(__name__)
_shell_probe_failures = metrics.get_meter(__name__).create_counter(
    "ava.inspect.shell_probe.failures", unit="{failure}"
)

# The compact-halt event name (payload key `body` mentions "compact") —
# mirrors `HALT_KEYS` in shared/events/contract.py.
HALT_EVENT = "halt"


def _alive_seconds(
    lifecycle_events: list[tuple[datetime, str]],
    window_start: datetime | None,
    spawned_at: datetime | None,
) -> float:
    """The agent's alive wall-clock, optionally clipped to a window lower bound.

    Sum of every spawn/resurrect → terminate interval (excluding gaps spent
    terminated); the open tail (currently alive) runs to now. Computed from the
    `agent_spawned` / `agent_resurrected` / `agent_terminated` lifecycle events.
    With `window_start` set, each interval is
    clipped to `[window_start, now]` before summing, so an interval that ended
    before the window contributes 0 — this is what makes the active-rate
    denominator follow the request window. `window_start=None` = whole life (no
    clip), the basis for `AgentTps.agent_lifecycle_tps`.

    Fallback: an agent from which no life START was recovered — no lifecycle
    events at all, OR a partial stream that predates `agent_spawned` emission
    and carries only a later `agent_terminated` — uses
    `agents_meta.spawned_at → now` instead, clipped the same way. The trigger
    is "no start seen", NOT "alive is 0": an agent WITH a start whose only life
    ended before `window_start` correctly reports 0 (it was not alive in the
    window) and must not fall back. A best-effort approximation that may
    overcount for resurrected/terminated agents (includes terminated gaps);
    real lifecycle events are more accurate. This is also the intentional
    bridge when inspect excludes pre-index-cutover lifecycle events: the open
    alive tail remains exact for the default 24-hour view, while whole-life
    and wide-window views can include pre-cutover gaps until legacy retention
    ends.

    Consecutive starts without an intervening terminate (the crash/reaper
    shape — SIGKILL/OOM leaves no `agent_terminated`, then the crash-resurrect
    controller wakes the row) close the previous interval at the new start
    instead of dropping it, so a resurrected agent keeps its pre-crash life.

    KNOWN GAP (crash/reaper): a process that died by SIGKILL/OOM emits no
    `agent_terminated` event, so an open lifecycle tail of an agent that is
    NEVER resurrected is counted to `now` and keeps growing — inflating alive
    and thus depressing active-rate for an abandoned-crashed agent. `not
    saw_start` fallback does NOT cover this (a crash HAS a start). Closing the
    tail at `agents_meta.status_changed_at` when the row is already
    `terminated`, and emitting a reaper-side terminate event, are the
    follow-ups (see PR)."""
    now = datetime.now(tz=UTC)

    def clip(start: datetime, end: datetime) -> float:
        """Seconds of `[start, end]` that fall inside `[window_start, now]`."""
        lo = start if window_start is None else max(start, window_start)
        hi = min(end, now)
        return max(0.0, (hi - lo).total_seconds())

    alive_seconds = 0.0
    saw_start = False
    current_life_start: datetime | None = None
    for ts, event in sorted(lifecycle_events):
        if event in ("agent_spawned", "agent_resurrected"):
            if current_life_start is not None:
                # A second start with no intervening terminate: the previous
                # life ended without an `agent_terminated` event — the normal
                # crash/reaper shape (SIGKILL/OOM leaves no finally, then the
                # crash-resurrect controller wakes the row). Closing the open
                # interval at the new start keeps that real life in the sum;
                # the resurrect timestamp is the best available bound for a
                # death whose true time is unknown (it can only overcount the
                # crash→resurrect gap, never drop a real interval).
                alive_seconds += clip(current_life_start, ts)
            saw_start = True
            current_life_start = ts
        elif event == "agent_terminated" and current_life_start is not None:
            alive_seconds += clip(current_life_start, ts)
            current_life_start = None
    # If the agent is currently alive (no terminating event after last start),
    # add time from the last start to now.
    if current_life_start is not None:
        alive_seconds += clip(current_life_start, now)

    if not saw_start and spawned_at is not None:
        alive_seconds = clip(spawned_at, now)
    return alive_seconds


def _agent_tps(
    values: _inspect_stats.InspectValues,
    spawned_at: datetime | None,
) -> AgentTps:
    """LM-stage and agent-lifecycle TPS for one agent.

    LM-stage TPS = output tokens / cumulative LLM call wall-clock (sum of
    `turn_end.duration_seconds` within the window). Isolates model generation
    speed excluding execute_code and framework overhead. Numerator and
    denominator come from the same shared ledger-plus-edge view.

    Agent-lifecycle TPS = output tokens / cumulative agent alive time (whole
    life, `_alive_seconds(window_start=None)` — the request window narrows only the
    numerator, so this stays a since-birth throughput)."""
    lm_stage_tps = (
        values.output_tokens / values.turn_duration_seconds
        if values.turn_duration_seconds > 0
        else 0.0
    )
    alive_seconds = _alive_seconds(
        values.lifecycle_events, window_start=None, spawned_at=spawned_at
    )
    agent_lifecycle_tps = values.output_tokens / alive_seconds if alive_seconds > 0 else 0.0

    return AgentTps(
        lm_stage_tps=round(lm_stage_tps, 2),
        agent_lifecycle_tps=round(agent_lifecycle_tps, 2),
    )


def _agent_activity(
    values: _inspect_stats.InspectValues,
    window_start: datetime | None,
    spawned_at: datetime | None,
) -> AgentActivity:
    """Active-rate for one agent: fraction of alive time spent actively working
    versus idle-waiting for input (see `AgentActivity` for the full contract).

    `active_seconds` = Σ `node_exit.duration_seconds` over every graph node
    EXCEPT `claim` within the window (llm + exec + hooks = all real processing).
    `llm_seconds` = Σ `turn_end.duration_seconds` within the window — the
    model's generation wall-clock (reasoning + output). `exec_seconds` = Σ
    `node_exit.duration_seconds` where node = 'exec'. `alive_seconds` =
    `_alive_seconds(window_start)`. `active_rate` = active/alive capped at 1.0;
    0.0 when alive is 0. Its inputs are prepared by the shared live pass."""
    active_rate = (
        values.active_seconds / alive_seconds
        if (alive_seconds := _alive_seconds(values.lifecycle_events, window_start, spawned_at)) > 0
        else 0.0
    )
    active_rate = min(1.0, active_rate)
    return AgentActivity(
        active_seconds=round(values.active_seconds, 2),
        alive_seconds=round(alive_seconds, 2),
        active_rate=round(active_rate, 4),
        llm_seconds=round(values.turn_duration_seconds, 2),
        exec_seconds=round(values.exec_seconds, 2),
    )


_HEARTBEAT_PAUSE_LOOKBACK = timedelta(hours=24)


def _heartbeat_last_pause(
    agent_id: int, *, deadline: float | None = None
) -> HeartbeatLastPause | None:
    """Newest recent heartbeat pause from Loki; safe to retain with aggregates.

    "Last pause" is a recent-history hint; `_inspect_live` reads authoritative
    active state from `agents_meta.heartbeat_paused_until`. The fixed 24-hour
    lookback matches the default cluster `heartbeat_pause_max_seconds` cap
    (86400), so any still-active default pause retains its start event. Raising
    the cap via `AVA_HEARTBEAT_PAUSE_MAX_SECONDS` or using a longer per-agent
    `config_overlay` is an accepted residual: the cell may show no recent pause
    while the authoritative active state remains correct. Older pauses likewise
    show as no recent pause, matching Loki's pre-existing 168-hour truncation.
    """
    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=["heartbeat_paused"],
        from_=datetime.now(tz=UTC) - _HEARTBEAT_PAUSE_LOOKBACK,
        limit=1,
        timeout_s=_query_timeout(deadline),
    )
    row = rows[0] if rows else None
    return (
        HeartbeatLastPause(at=row["ts"], duration_s=row["attributes"].get("duration_s"))
        if row is not None
        else None
    )


def _heartbeat_last_pause_or_none(agent_id: int) -> HeartbeatLastPause | None:
    """Best-effort recent pause for the fast live endpoint.

    The authoritative active pause comes from Postgres. A Loki transport or
    admission failure must not take down the cheap inspector skeleton merely
    because its optional historical hint is unavailable.
    """
    try:
        return _heartbeat_last_pause(agent_id)
    except (httpx.HTTPError, ValueError):
        return None


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
    server on a split runner has no DB access). A session without a row has
    no TTL — watcher sessions deliberately record none, and legacy
    pre-mandate shells predate the table.
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
            s.model_copy(update={"expires_at": ttls.get(s.id)}) if s.id in ttls else s
            for s in shells
        ]
    return shells, True


class _InspectAggregates(NamedTuple):
    """Only Loki/ledger-derived inspector sections retained by the TTL.

    Machine/config/heartbeat/liveness and all timestamps come from a fresh
    agents_meta read on every HTTP request. Keeping that boundary explicit
    prevents a latency cache from becoming a stale control-plane snapshot.
    """

    cost: Any
    stats: Any
    tps: Any
    activity: Any
    heartbeat_last_pause: HeartbeatLastPause | None


# Distinct-key inspect loads admit one leader per Loki query slot. Their three
# independent sections run on one process-lifetime executor, which stays bounded
# even when a timed-out caller has released its response task.
_INSPECT_MAX_CONCURRENT_LOADS = 4
_INSPECT_EXECUTOR_WORKERS = 4
_INSPECT_SINGLEFLIGHT_MAX = 32
_inspect_executor = ThreadPoolExecutor(
    max_workers=_INSPECT_EXECUTOR_WORKERS,
    thread_name_prefix="inspect",
)


def _remaining_timeout(deadline: float | None) -> float | None:
    """Return the remaining load budget or abort once its deadline elapsed."""
    if deadline is None:
        return None
    remaining = deadline - time_mod.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _discard_future_exception(future: Future[Any]) -> None:
    """Consume a late section exception after the load deadline has elapsed."""
    if not future.cancelled():
        future.exception()


def _inspect_blocking(
    pool: ConnectionPool[Any],
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
    spawned_at: datetime | None,
    deadline: float | None = None,
) -> _InspectAggregates:
    """Sync twin of the inspect endpoint's event-history section — runs via
    asyncio.to_thread so the event loop stays free. Its sections run on the
    shared bounded executor and stop waiting when the load deadline expires."""
    _remaining_timeout(deadline)
    # `window_start` is the concrete lower-bound instant of the request
    # window — the active-rate denominator clips alive-time to it (alive
    # is replayed in Python). since_compact → the compact halt ts (or
    # None when never compacted); hours → now - N h; neither → None
    # (whole life).
    from_, window_start = window_bounds(
        agent_id,
        hours,
        since_compact=since_compact,
        deadline=deadline,
    )
    # Keep the leader in asyncio.to_thread's default executor. Submitting a
    # leader that waits on section futures here would deadlock this pool when
    # every shared worker is occupied by leaders.
    f_cost = _inspect_executor.submit(
        _agent_cost.agent_cost,
        pool,
        agent_id,
        hours,
        since_compact=since_compact,
        deadline=deadline,
    )
    f_values = _inspect_executor.submit(
        _inspect_stats.inspect_values,
        pool,
        agent_id,
        from_,
        None,
        deadline=deadline,
    )
    f_heartbeat_last_pause = _inspect_executor.submit(
        _heartbeat_last_pause,
        agent_id,
        deadline=deadline,
    )
    futures = (f_cost, f_values, f_heartbeat_last_pause)
    try:
        cost = f_cost.result(timeout=_remaining_timeout(deadline))
        values = f_values.result(timeout=_remaining_timeout(deadline))
        tps = _agent_tps(values, spawned_at)
        activity = _agent_activity(values, window_start, spawned_at)
        heartbeat_last_pause = f_heartbeat_last_pause.result(timeout=_remaining_timeout(deadline))
    except FutureTimeoutError as exc:
        for future in futures:
            future.add_done_callback(_discard_future_exception)
        raise TimeoutError from exc
    return _InspectAggregates(
        cost=cost,
        stats=values.stats,
        tps=tps,
        activity=activity,
        heartbeat_last_pause=heartbeat_last_pause,
    )


# (agent_id, hours, since_compact) -> (monotonic expiry, _InspectAggregates).
# The event-history aggregates are the panel's expensive half (a whole-life
# call combines cost, heartbeat, and one shared projected Loki pass), and the frontend
# refetches them in bursts — on every panel open (refetchOnMount:always), on
# every notice SSE event for the agent, and on the 60s background interval.
# A 75s TTL spans one 60s open-panel poll tick, so the static retention-window
# scan is never repeated on every tick. Live DB state, `notice`, and `shells`
# never ride the cache (see the endpoint). Bound the dict and prune on overflow.
_INSPECT_CACHE_TTL_S = 75.0
_INSPECT_CACHE_MAX = 1024
# A panel request is an interactive read, not a batch job. Until the unlabeled
# pre-cutover Loki slice expires on 2026-08-30 11:10Z, a cold wide-window or whole-life
# load can legitimately take about 15 seconds; 30 seconds prevents those reads
# from returning 503. Every Loki query remains individually bounded at 8 seconds,
# so a down backend fails in about 16 seconds rather than waiting for this bound.
# The response budget is also the leader deadline: a timed-out request releases
# admission once its fan-out stops rather than later populating this cache.
_INSPECT_RESPONSE_TIMEOUT_S = 30.0
_InspectKey = tuple[int, int | None, bool]
_inspect_query_cache = InspectQueryCache[_InspectKey, _InspectAggregates](
    max_entries=_INSPECT_CACHE_MAX,
    max_inflight=_INSPECT_SINGLEFLIGHT_MAX,
    max_concurrent_loads=_INSPECT_MAX_CONCURRENT_LOADS,
)


def _inspect_rows_cached(
    pool: ConnectionPool[Any],
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
    spawned_at: datetime | None = None,
) -> _InspectAggregates:
    """Return cached rows or share one in-flight fan-out for this exact key."""
    key: _InspectKey = (agent_id, None if hours is None else int(hours), since_compact)
    deadline = time_mod.monotonic() + _INSPECT_RESPONSE_TIMEOUT_S
    try:
        return _inspect_query_cache.get_or_load(
            key,
            lambda: _inspect_blocking(
                pool,
                agent_id,
                hours,
                since_compact=since_compact,
                spawned_at=spawned_at,
                deadline=deadline,
            ),
            ttl_s=_INSPECT_CACHE_TTL_S,
            now=time_mod.monotonic,
        )
    except InspectCacheFullError as exc:
        raise HTTPException(status_code=503, detail="inspect query queue is full") from exc


async def _inspect_rows_cached_async(
    pool: ConnectionPool[Any],
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
    spawned_at: datetime | None = None,
) -> _InspectAggregates:
    """Async request twin: followers await single-flight without a worker."""
    key: _InspectKey = (agent_id, None if hours is None else int(hours), since_compact)
    deadline = time_mod.monotonic() + _INSPECT_RESPONSE_TIMEOUT_S
    try:
        return await _inspect_query_cache.get_or_load_async(
            key,
            lambda: _inspect_blocking(
                pool,
                agent_id,
                hours,
                since_compact=since_compact,
                spawned_at=spawned_at,
                deadline=deadline,
            ),
            ttl_s=_INSPECT_CACHE_TTL_S,
            now=time_mod.monotonic,
        )
    except InspectCacheFullError as exc:
        raise HTTPException(status_code=503, detail="inspect query queue is full") from exc


def cache_clear() -> None:
    """Test seam: drop the inspect response cache."""
    _inspect_query_cache.clear()
    _inspect_stats.reset_for_tests()


@router.get("/api/agents/{agent_id}/inspect/live", response_model=AgentInspectLive)
async def get_agent_inspect_live(agent_id: int, request: Request) -> AgentInspectLive:
    """Cheap current-state half of the inspector panel.

    Reads the agent projection and open notice from Postgres, probes shells on
    the owning runner, and performs only one bounded best-effort Loki lookup for
    the heartbeat's recent-pause hint. Unknown agents return 404. Shell probe
    failures set shells_available=False and Loki failures degrade
    `heartbeat.last_pause` to None, keeping this endpoint useful as the panel's
    fast skeleton source. No part of this response is cached.
    """
    pool = request.app.state.db_pool
    db = await asyncio.to_thread(db_rows_blocking, pool, agent_id)
    notice, shells, last_pause = await asyncio.gather(
        asyncio.to_thread(notice_blocking, pool, agent_id),
        _probe_agent_shells(agent_id, db.machine, pool),
        asyncio.to_thread(_heartbeat_last_pause_or_none, agent_id),
    )
    return AgentInspectLive(
        agent_id=agent_id,
        machine=db.machine,
        liveness_state=db.liveness_state,
        last_probe_at=db.last_probe_at,
        spawned_at=db.spawned_at,
        started_at=db.started_at,
        shells=shells[0],
        shells_available=shells[1],
        observation=db.observation,
        config_overlay=db.config_overlay,
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


@router.get("/api/agents/{agent_id}/inspect")
async def get_agent_inspect(
    agent_id: int,
    request: Request,
    hours: Annotated[StatsWindowHours | None, Query()] = None,
    since_compact: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> AgentInspect:
    """Per-agent inspector panel data in one shot — the agent's live persistent
    shells, its frozen config overlay, its LLM cost, turn/exec stats, idle
    heartbeat state. The
    single-agent counterpart to `/api/stats/dashboard` (fleet-wide).

    `?hours=` windows `cost` + `stats` to the selected range (0 = last 5m;
    1/6/24/72/168 = hours, anything else 422s), clamped to Loki retention;
    omitted = cumulative since spawn. `applied_window_hours` reports the
    served horizon while `window_hours` continues to echo the request.
    `?since_compact=true` windows them to events since the agent's latest
    compact halt instead — it takes precedence, `hours` is ignored and the
    echoed `window_hours` is None.
    `shells` + `config_overlay` + `heartbeat` are always
    current, independent of the window. 404 if the agent is unknown (no
    agents_meta row). `config_overlay` is the spawn-time override map — `{}` when
    the agent runs on cluster defaults (the column is NULL).

    Latency discipline: event-history sections use a shared bounded executor,
    and only their aggregates ride a 75s TTL cache keyed by (agent_id, hours,
    since_compact). Concurrent misses share one single-flight Future; no more
    than `_INSPECT_MAX_CONCURRENT_LOADS` distinct leaders run at once, and a
    saturated request gets the queue-full 503. Each leader's 15-second response
    budget is also its load deadline, so expired work stops and releases its
    admission slot rather than continuing under transport timeouts. The
    agents_meta projection (machine, config, heartbeat inputs, liveness and
    timestamps), `notice`, and `shells` are fetched fresh on every call and
    never ride the cache. `shells` is probed on the agent's own machine via the
    `shell_probe` cluster op (the gateway never runs sessions itself; every
    machine — its own included — is dialed at its registered ops URL), so a
    split deployment reflects each agent's runner and an unreachable machine
    sets shells_available=False rather than claiming no shells exist.
    `heartbeat` is the agent's idle check-in state: the
    projected next check-in due time when idle (or the active pause / running
    suppression) plus its most recent pause from history.

    The retained live lifecycle leg begins at the index-label cutover and never
    scans the legacy slice. It has an 8-second Loki timeout and a per-agent
    thirty-minute single-flight cache, so changing `hours` or `since_compact`
    does not repeat that indexed read. Agents whose lifecycle history is wholly
    pre-cutover use the `spawned_at` fallback described by `_alive_seconds`.
    """
    pool = request.app.state.db_pool
    applied_window_hours = None if since_compact or hours is None else applied_window(hours)[0]
    # Release the agents_meta borrow before entering the potentially queued
    # Loki fan-out. This fresh read is the live half of the response and must
    # execute even when the historical aggregate is a TTL hit.
    db = await asyncio.to_thread(db_rows_blocking, pool, agent_id)
    try:
        aggregates = await asyncio.wait_for(
            _inspect_rows_cached_async(
                pool,
                agent_id,
                hours,
                since_compact=since_compact,
                spawned_at=db.spawned_at,
            ),
            timeout=_INSPECT_RESPONSE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="inspector history query timed out; retry",
            headers={"Retry-After": "1"},
        ) from exc
    except loki_query_budget.LokiQueryBudgetError:
        # Preserve the process-wide admission handler's machine-readable reason.
        raise
    except httpx.HTTPError as exc:
        raise_backend_unavailable(exc)
    # The notice is read fresh on every call (it never rides the cache): the
    # panel's reply surface must clear the moment a notice resolves, and the
    # SELECT is cheap. The shell probe is equally cheap and always live.
    notice = await asyncio.to_thread(notice_blocking, pool, agent_id)
    shells, shells_available = await _probe_agent_shells(agent_id, db.machine, pool)
    return AgentInspect(
        agent_id=agent_id,
        machine=db.machine,
        spawned_at=db.spawned_at,
        started_at=db.started_at,
        window_hours=None if since_compact else hours,
        applied_window_hours=applied_window_hours,
        since_compact=since_compact,
        shells=shells,
        shells_available=shells_available,
        observation=db.observation,
        config_overlay=db.config_overlay,
        notice=notice,
        cost=aggregates.cost,
        stats=aggregates.stats,
        tps=aggregates.tps,
        activity=aggregates.activity,
        heartbeat=project_heartbeat(
            status=db.status,
            last_active_at=db.last_active_at,
            last_heartbeat_at=db.last_heartbeat_at,
            paused_until=db.paused_until,
            agent_id=agent_id,
            pending_inbound=db.pending_inbound,
            last_pause=aggregates.heartbeat_last_pause,
        ),
        liveness_state=db.liveness_state,
        last_probe_at=db.last_probe_at,
    )


@router.get("/api/agents/{agent_id}/neighbors")
def get_agent_neighbors(
    agent_id: int,
    request: Request,
    depth: Annotated[int, Query(ge=1, le=5)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> NeighborsResponse:
    """The agents most strongly tied to `agent_id`, ranked by recency-weighted
    interaction strength (spawn / fork / resurrect / message, all equal weight),
    plus `ancestors` — the immutable birth chain above `agent_id`, nearest ancestor first.

    `depth=1` returns direct ties only; a higher `depth` follows ties outward,
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
