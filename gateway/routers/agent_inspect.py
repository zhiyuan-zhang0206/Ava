"""Per-agent inspector panel — GET /api/agents/{id}/inspect.

The single-agent counterpart to `/api/stats/dashboard` (which aggregates the
whole fleet): one agent's live persistent shells, its frozen config overlay,
its LLM cost, and turn/exec statistics. cost/stats are aggregations over the
unified event stream: the live tail reads Loki (the LGTM read side, task
#1197), and the pre-cutover history reads the frozen PG `events` archive —
the two are stitched at the archive's freeze point so whole-life cost/tokens
stay cumulative (task #1273). By default they cover the agent's whole life
("the agent's session" is its whole life — agent_id is stable across
restart/resurrect); `?hours=` narrows them to a recent window. `shells` is
probed on the agent's own machine via the `shell_probe` cluster op — the
gateway never touches sessions itself, and every machine (the gateway's own
included) is dialed at its registered ops URL, so a remote runner's shells
are visible exactly like a local one's.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, NamedTuple, cast

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg_pool import ConnectionPool

from gateway import loki_events
from gateway.routers import _agent_cost, _plugin_metrics
from gateway.routers._agent_cost import _from_bound
from gateway.schemas import (
    AgentActivity,
    AgentInspect,
    AgentStats,
    AgentTps,
    HeartbeatInfo,
    HeartbeatLastPause,
    NeighborRow,
    NeighborsResponse,
    PluginMetricResult,
    StatsWindowHours,
)
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import ShellInfo
from shared.agent_snapshot import OpenNotice
from shared.agents import AgentNotFound, AgentStatus
from shared.config import settings
from shared.db import NOTICE_FYI_TTL_DAYS

router = APIRouter()

# The compact-halt event name (payload key `body` mentions "compact") —
# mirrors `HALT_KEYS` in shared/events/contract.py.
HALT_EVENT = "halt"


# Shared filter kwargs for the loki_events aggregate calls — a TypedDict so
# `**common` unpacks with exact types under pyright strict.
def _agent_stats(agent_id: int, from_: datetime | None, to: datetime | None) -> AgentStats:
    """Turn + exec counters for one agent, over the window.

    `turn_ok` is turns with ok=true; the percentiles / min / max come from
    the Loki payload-attribute aggregates over every turn's
    `duration_seconds` (client-side percentile, `percentile_cont` semantics).
    exec failure is every exec outcome other than plain `exec`
    (exec_failed / exec_thread_stuck / exec(timeout)) — same exec-ok /
    exec-failed split as the metrics report.
    """
    common: _agent_cost._AggCommon = {
        "event_names": ["turn_end"],
        "categories": ["telemetry", "log"],
        "agent_id": agent_id,
        "from_": from_,
        "to": to,
    }
    turn_total = loki_events.count_events(**common)
    turn_ok = loki_events.count_events(**common, attribute_filters={"ok": "true"})
    p50 = loki_events.attribute_aggregate(
        field="duration_seconds", agg="quantile", quantile=0.5, **common
    )
    p90 = loki_events.attribute_aggregate(
        field="duration_seconds", agg="quantile", quantile=0.9, **common
    )
    tmin = loki_events.attribute_aggregate(field="duration_seconds", agg="min", **common)
    tmax = loki_events.attribute_aggregate(field="duration_seconds", agg="max", **common)

    exec_ok = loki_events.count_events(
        event_names=["^exec$"],
        categories=["telemetry", "log"],
        agent_id=agent_id,
        from_=from_,
        to=to,
    )
    exec_failed = loki_events.count_events(
        event_names=["^exec_.*", "^exec\\(.*"],
        categories=["telemetry", "log"],
        agent_id=agent_id,
        from_=from_,
        to=to,
    )
    return AgentStats(
        turn_total=int(turn_total),
        turn_ok=int(turn_ok),
        turn_p50_seconds=round(float(p50), 2),
        turn_p90_seconds=round(float(p90), 2),
        turn_min_seconds=round(float(tmin), 2),
        turn_max_seconds=round(float(tmax), 2),
        exec_ok=int(exec_ok),
        exec_failed=int(exec_failed),
    )


def _alive_seconds(
    agent_id: int, window_start: datetime | None, spawned_at: datetime | None
) -> float:
    """The agent's alive wall-clock, optionally clipped to a window lower bound.

    Sum of every spawn/resurrect → terminate interval (excluding gaps spent
    terminated); the open tail (currently alive) runs to now. Computed from the
    `agent_spawned` / `agent_resurrected` / `agent_terminated` lifecycle events
    (read from Loki, chronological). With `window_start` set, each interval is
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
    real lifecycle events are more accurate.

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

    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=["agent_spawned", "agent_resurrected", "agent_terminated"],
        from_=_agent_cost._whole_life_start(),
        limit=1000,
    )
    lifecycle_events: list[tuple[str, datetime]] = [
        (r["event_name"], r["ts"]) for r in reversed(rows)
    ]

    alive_seconds = 0.0
    saw_start = False
    current_life_start: datetime | None = None
    for event, ts in lifecycle_events:
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
    agent_id: int,
    output_tokens: int,
    from_: datetime | None,
    to: datetime | None,
    spawned_at: datetime | None,
) -> AgentTps:
    """LM-stage and agent-lifecycle TPS for one agent.

    LM-stage TPS = output tokens / cumulative LLM call wall-clock (sum of
    `turn_end.duration_seconds` within the window). Isolates model generation
    speed excluding execute_code and framework overhead.

    Agent-lifecycle TPS = output tokens / cumulative agent alive time (whole
    life, `_alive_seconds(window_start=None)` — the window narrows only the
    numerator, so this stays a since-birth throughput)."""
    llm_seconds = loki_events.attribute_aggregate(
        field="duration_seconds",
        agg="sum",
        event_names=["turn_end"],
        categories=["telemetry", "log"],
        agent_id=agent_id,
        from_=from_,
        to=to,
    )
    lm_stage_tps = output_tokens / llm_seconds if llm_seconds > 0 else 0.0

    alive_seconds = _alive_seconds(agent_id, window_start=None, spawned_at=spawned_at)
    agent_lifecycle_tps = output_tokens / alive_seconds if alive_seconds > 0 else 0.0

    return AgentTps(
        lm_stage_tps=round(lm_stage_tps, 2),
        agent_lifecycle_tps=round(agent_lifecycle_tps, 2),
    )


def _agent_activity(
    agent_id: int,
    from_: datetime | None,
    to: datetime | None,
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
    0.0 when alive is 0."""
    active_seconds = loki_events.attribute_aggregate(
        field="duration_seconds",
        agg="sum",
        event_names=["node_exit"],
        agent_id=agent_id,
        from_=from_,
        to=to,
        attribute_filters={"node": "!=claim"},
    )
    active_rate = (
        active_seconds / alive_seconds
        if (alive_seconds := _alive_seconds(agent_id, window_start, spawned_at)) > 0
        else 0.0
    )
    active_rate = min(1.0, active_rate)

    llm_seconds = loki_events.attribute_aggregate(
        field="duration_seconds",
        agg="sum",
        event_names=["turn_end"],
        categories=["telemetry", "log"],
        agent_id=agent_id,
        from_=from_,
        to=to,
    )
    exec_seconds = loki_events.attribute_aggregate(
        field="duration_seconds",
        agg="sum",
        event_names=["node_exit"],
        agent_id=agent_id,
        from_=from_,
        to=to,
        attribute_filters={"node": "=exec"},
    )
    return AgentActivity(
        active_seconds=round(active_seconds, 2),
        alive_seconds=round(alive_seconds, 2),
        active_rate=round(active_rate, 4),
        llm_seconds=round(llm_seconds, 2),
        exec_seconds=round(exec_seconds, 2),
    )


def _heartbeat(
    agent_id: int,
    status: str,
    last_active_at: datetime,
    paused_until: datetime | None,
    *,
    pending_inbound: bool,
) -> HeartbeatInfo:
    """Idle-heartbeat state for one agent — the projected next check-in (or the
    active pause / already-queued wake) plus the most recent pause from history.

    `paused_until` is the agents_meta column already narrowed by the caller to a
    *future* value (a NULL / past pause arrives here as None = not paused). The
    display states are mutually exclusive:

    - idle, paused → `paused_until` (the active suppression end).
    - idle, not paused, a wake already queued → `heartbeat_pending` (see below).
    - idle, not paused, nothing queued → `next_at`, the daemon's next check-in
      time `last_active_at + heartbeat_idle_threshold_seconds`, matching the
      daemon's due-time projection off the real-activity clock.
    - any non-idle agent → none of them (it never gets a check-in while active).

    `heartbeat_pending` mirrors the daemon's `NOT EXISTS (pending inbound)` guard:
    the daemon checks in on an idle agent only when it has no inbound already
    pending, so while one *is* pending (a heartbeat check-in already sent but not
    yet processed, or a chat about to wake it) the daemon will not schedule
    another and there is no future check-in to project. Surfacing this state —
    instead of projecting `next_at` off a stale clock — is what keeps a stuck
    agent (an unconsumed check-in it never woke to process) from rendering a
    nonsensical past "next heartbeat" time. When no inbound is pending, any
    earlier check-in was already consumed (its turn bumped `last_active_at` past
    it), so `last_active_at` alone is the correct projection basis — no
    `heartbeat_nudged`-event floor is needed.

    `last_pause` is the newest `heartbeat_paused` event under this agent_id,
    independent of current state (read from Loki).
    """
    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=["heartbeat_paused"],
        from_=_agent_cost._whole_life_start(),
        limit=1,
    )
    row = rows[0] if rows else None
    last_pause = (
        HeartbeatLastPause(at=row["ts"], duration_s=row["attributes"].get("duration_s"))
        if row is not None
        else None
    )

    # The daemon projects the next check-in from last_active_at + idle_threshold_s
    # (plus per-agent jitter). Use idle_threshold_s, not heartbeat_interval_seconds
    # (the poll cadence), so the inspector matches the daemon's contract.
    # heartbeat_interval_seconds is kept for the return value — the frontend badge
    # ("every 5m") uses it as the configured check-in cadence.
    idle_threshold_s = int(settings.daemon.heartbeat_idle_threshold_seconds)
    interval_s = int(settings.daemon.heartbeat_interval_seconds)

    next_at: datetime | None = None
    active_pause: datetime | None = None
    heartbeat_pending = False
    if status == AgentStatus.IDLING:
        if paused_until is not None:
            active_pause = paused_until
        # Mirror the daemon's `NOT EXISTS (pending inbound)` guard: with a
        # wake already queued, no check-in is scheduled (heartbeat_pending);
        # with nothing queued, project from the idle clock.
        # `pending_inbound` is computed by the caller on the same DB
        # connection (the inspector's agents_meta read).
        elif pending_inbound:
            heartbeat_pending = True
        else:
            next_at = last_active_at + timedelta(seconds=idle_threshold_s)
    return HeartbeatInfo(
        interval_s=interval_s,
        next_at=next_at,
        paused_until=active_pause,
        heartbeat_pending=heartbeat_pending,
        last_pause=last_pause,
    )


# Per-op probe deadline for the inspector's shell list. The panel polls every
# 5s; a reachable runner answers a shell_probe in milliseconds, and an
# unreachable one fails the connect within this bound (same budget as the
# roster's status_probe). On failure the inspector shows an empty shell list
# rather than failing the whole panel — a down runner is a liveness problem the
# roster reports, not something the inspector should 503 on.
_SHELL_PROBE_TIMEOUT_S = 3.0


async def _probe_agent_shells(agent_id: int, machine: str) -> list[ShellInfo]:
    """The agent's live persistent shells, probed on the machine it runs on.

    One uniform path for every machine — the gateway never probes sessions itself.
    `machine` is `agents_meta.machine` (the physical host the agent was spawned
    on; 'unknown' only for legacy rows), resolved through the `machines` table
    to that host's ops server URL and asked via the `shell_probe` op; the
    gateway's own box is just another row (its localhost URL), so a
    single-box deployment dials itself exactly like a split deployment dials a
    runner.

    Degrades to an empty list on any probe failure (unreachable machine,
    unregistered name, version-skewed runner that does not know the op): the
    shell section reads "None open" instead of the whole inspector 503ing on a
    machine problem that the roster already surfaces.
    """
    try:
        result = await _cluster_rpc.dispatch_to_machine(
            machine, "shell_probe", {"agent_id": agent_id}, timeout_s=_SHELL_PROBE_TIMEOUT_S
        )
    except (_cluster_rpc.ClusterOpUnreachable, _cluster_rpc.ClusterOpFailed):
        return []
    return [ShellInfo.model_validate(s) for s in result.get("shells", [])]


class _InspectRows(NamedTuple):
    """Everything the inspect endpoint reads from the DB — assembled off the
    event loop (the queries are synchronous psycopg; the frontend polls this
    endpoint every 5s, and a DB stall on the loop would freeze the gateway)."""

    machine: str
    spawned_at: Any
    started_at: Any
    config_overlay: dict[str, Any]
    notice: OpenNotice | None
    cost: Any
    stats: Any
    tps: Any
    activity: Any
    heartbeat: Any
    liveness_state: Literal["online", "offline", "unknown"]
    last_probe_at: Any


def _inspect_blocking(
    pool: ConnectionPool[Any], agent_id: int, hours: StatsWindowHours | None, *, since_compact: bool
) -> _InspectRows:
    """Sync twin of the inspect endpoint's DB section — runs via
    asyncio.to_thread so the event loop stays free (5s frontend poll)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, machine, status, last_active_at, "
            "       spawned_at, started_at, "
            "       CASE WHEN heartbeat_paused_until > now() THEN heartbeat_paused_until END, "
            "       liveness_state, last_probe_at "
            "FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        config_overlay = row[0] if row[0] is not None else {}
        machine = row[1]
        spawned_at = row[4]
        started_at = row[5]
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND status = 'pending')",
            (agent_id,),
        )
        pending_row = cur.fetchone()
        assert pending_row is not None  # noqa: S101 — EXISTS always returns one row
        heartbeat = _heartbeat(
            agent_id,
            status=row[2],
            last_active_at=row[3],
            paused_until=row[6],
            pending_inbound=bool(pending_row[0]),
        )
        liveness_state = cast(
            Literal["online", "offline", "unknown"],
            row[7] if row[7] is not None else "unknown",
        )
        last_probe_at = row[8]
        # The agent's single open notice (since #152 at most one) — may be
        # either kind (require_response or FYI). None when the agent has none.
        # Expired FYI notices (older than NOTICE_FYI_TTL_DAYS, audit C1) are
        # excluded like everywhere else; require_response never expires.
        cur.execute(
            "SELECT id, title, content, priority, require_response, blocking, created_at "
            "FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL "
            "AND (require_response OR created_at > now() - make_interval(days => %s)) "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id, NOTICE_FYI_TTL_DAYS),
        )
        nr = cur.fetchone()
        notice = (
            OpenNotice(
                id=nr[0],
                title=nr[1],
                content=nr[2],
                priority=nr[3],
                require_response=nr[4],
                blocking=nr[5],
                created_at=nr[6],
            )
            if nr
            else None
        )
        # `window_start` is the concrete lower-bound instant of the request
        # window — the active-rate denominator clips alive-time to it (alive
        # is replayed in Python). since_compact → the compact halt ts (or
        # None when never compacted); hours → now - N h; neither → None
        # (whole life).
        from_, window_start = _from_bound(agent_id, hours, since_compact=since_compact)
        cost_result = _agent_cost._agent_cost(pool, agent_id, from_, window_start)
        cost = cost_result.cost
        stats = _agent_stats(agent_id, from_, None)
        # TPS pairs the Loki-window tokens with the Loki-only turn-end
        # denominator (see _CostResult); the archive side of `cost` covers
        # tokens the turn-end denominator never sees.
        tps = _agent_tps(agent_id, cost_result.loki_tokens_out, from_, None, spawned_at)
        activity = _agent_activity(agent_id, from_, None, window_start, spawned_at)
    return _InspectRows(
        machine=machine,
        spawned_at=spawned_at,
        started_at=started_at,
        config_overlay=config_overlay,
        notice=notice,
        cost=cost,
        stats=stats,
        tps=tps,
        activity=activity,
        heartbeat=heartbeat,
        liveness_state=liveness_state,
        last_probe_at=last_probe_at,
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

    `?hours=` windows `cost` + `stats` to the past N hours (whitelisted to
    1/6/24/72/168, anything else 422s); omitted = cumulative since spawn.
    `?since_compact=true` windows them to events since the agent's latest
    compact halt instead — it takes precedence, `hours` is ignored and the
    echoed `window_hours` is None.
    `shells` + `config_overlay` + `heartbeat` are always
    current, independent of the window. 404 if the agent is unknown (no
    agents_meta row). `config_overlay` is the spawn-time override map — `{}` when
    the agent runs on cluster defaults (the column is NULL). `shells` is probed
    on the agent's own machine via the `shell_probe` cluster op (the gateway
    never runs sessions itself; every machine — its own included — is dialed at its
    registered ops URL), so a split deployment reflects each agent's runner and
    an unreachable machine degrades to an empty list rather than failing the
    panel. `heartbeat` is the agent's idle check-in state: the
    projected next check-in when idle (or the active pause / running suppression)
    plus its most recent pause from history.
    """
    rows = await asyncio.to_thread(
        _inspect_blocking,
        request.app.state.db_pool,
        agent_id,
        hours,
        since_compact=since_compact,
    )
    return AgentInspect(
        agent_id=agent_id,
        machine=rows.machine,
        spawned_at=rows.spawned_at,
        started_at=rows.started_at,
        window_hours=None if since_compact else hours,
        since_compact=since_compact,
        shells=await _probe_agent_shells(agent_id, rows.machine),
        config_overlay=rows.config_overlay,
        notice=rows.notice,
        cost=rows.cost,
        stats=rows.stats,
        tps=rows.tps,
        activity=rows.activity,
        heartbeat=rows.heartbeat,
        liveness_state=rows.liveness_state,
        last_probe_at=rows.last_probe_at,
    )


@router.get("/api/agents/{agent_id}/neighbors")
def get_agent_neighbors(
    agent_id: int,
    request: Request,
    depth: Annotated[int, Query(ge=1, le=5)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> NeighborsResponse:
    """The agents most strongly tied to `agent_id`, ranked by recency-weighted
    interaction strength (spawn / fork / resurrect / message, all equal weight).

    `depth=1` returns direct ties only; a higher `depth` follows ties outward,
    discounting each extra hop. Terminated agents are included (each row carries
    `status`); `limit` caps the count, strongest first. The strength + decay are
    computed by the `agent_neighbors` SQL function — fleet-wide read, role-neutral.

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
    """
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (agent_id,))
        if cur.fetchone() is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        cur.execute(
            """
            SELECT n.agent_id, t.label, m.status, n.depth, n.score
            FROM agent_neighbors(%s, %s, 0.5, 0.5, %s) n
            JOIN agents t      ON t.id = n.agent_id
            JOIN agents_meta m ON m.id = n.agent_id
            ORDER BY n.score DESC
            """,
            (agent_id, depth, limit),
        )
        neighbors = [
            NeighborRow(agent_id=r[0], label=r[1], status=r[2], depth=r[3], score=round(r[4], 4))
            for r in cur.fetchall()
        ]
    return NeighborsResponse(neighbors=neighbors)


@router.get("/api/agents/{agent_id}/inspect/metrics")
async def get_agent_plugin_metrics(agent_id: int, request: Request) -> list[PluginMetricResult]:
    """The agent's plugin metrics for the inspector panel — the W13b inspector
    surface of the plugin metric system (see `shared/plugin_metrics.py`).

    Reads the registry snapshot the generator exports
    ($AVA_HOME/state/plugin_metrics.json), keeps the metrics whose `output`
    includes "inspector", renders each template for this agent ({{agent_id}}
    -> ``agent_id = <n>``), re-validates the rendered SQL (the persisted file
    is not trusted), substitutes the Grafana time macros with a fixed recent
    window (24h in 1h buckets), and executes each query read-only against the
    cluster's Postgres.

    Response: one `PluginMetricResult` per registered inspector metric, in
    registration order. `timeseries` / `barchart` metrics carry `series`
    (bucket ts + value, chronological); `stat` metrics carry `value`. A
    metric whose query fails at runtime carries `error` (the others still
    render); registry-level problems are HTTP errors instead — missing file ->
    200 [] (nothing registered yet), unreadable/invalid file or a template
    failing the safety re-validation -> 500 with the reason, {{agent_id}}
    template without an agent id -> 400 (unreachable here — the id is a path
    param). 404 when the agent does not exist. The frontend panel polls this
    every 5s like the parent /inspect. Implementation in
    ``gateway/routers/_plugin_metrics.py``.
    """
    return await asyncio.to_thread(
        _plugin_metrics.metrics_for_agent, request.app.state.db_pool, agent_id
    )
