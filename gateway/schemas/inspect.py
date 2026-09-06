"""agent inspect + per-agent cost/stats/neighbors.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
)

from gateway.schemas.stats import StatsWindowHours
from ops.rpc_schemas import ShellInfo
from shared.agent_observation import AgentObservation
from shared.agent_snapshot import OpenNotice


class AgentCost(BaseModel):
    """LLM spend + token usage for one agent over the requested window (whole
    life = ledger days + today's live tail; every `llm_usage` event under its
    agent_id — the agent's "session", which spans restarts/resurrects since
    agent_id is stable). `cost_usd` sums the rows' usage-time price snapshots
    only — never re-priced against the current registry. Calls without a
    snapshot (unpriced model) contribute 0 to cost and are counted in
    `unpriced_calls`. `cache_hit_pct` = cache_read / input * 100; input=0
    degrades to 0."""

    model_config = ConfigDict(frozen=True)

    cost_usd: float = Field(ge=0)
    unpriced_calls: NonNegativeInt
    llm_calls: NonNegativeInt
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    tokens_cached: NonNegativeInt
    tokens_reasoning: NonNegativeInt
    cache_hit_pct: float = Field(ge=0, le=100)


class AgentStats(BaseModel):
    """Cumulative turn + exec counters for one agent (all-time, no window).

    `turn_ok` counts `turn_end` events with ok=true (abnormal/cancelled turns
    excluded); the p50/p90/max are over every turn's `duration_seconds`.
    `exec_ok` is plain `exec` events; `exec_failed` is every other exec outcome
    (exec_failed / exec(timeout) / exec_cancelled; the prefix regex also
    counts legacy exec_thread_stuck rows for historical continuity)."""

    model_config = ConfigDict(frozen=True)

    turn_total: NonNegativeInt
    turn_ok: NonNegativeInt
    turn_p50_seconds: float = Field(ge=0)
    turn_p90_seconds: float = Field(ge=0)
    turn_min_seconds: float = Field(ge=0)
    turn_max_seconds: float = Field(ge=0)
    exec_ok: NonNegativeInt
    exec_failed: NonNegativeInt


class AgentTps(BaseModel):
    """Token-per-second metrics for one agent — two views of throughput.

    `lm_stage_tps` = output tokens / cumulative LLM-call wall-clock,
    isolating just the model's generation speed excluding execute_code and framework
    overhead. Denominator is the sum of all `turn_end.duration_seconds` over the window.

    `agent_lifecycle_tps` = output tokens / agent's cumulative alive
    wall-clock, covering the full agent runtime including non-LLM time.
    Denominator is the agent's total time in non-terminated status (sum of each
    spawn/resurrect → terminate interval, excluding gaps spent terminated)."""

    model_config = ConfigDict(frozen=True)

    lm_stage_tps: float = Field(ge=0)
    agent_lifecycle_tps: float = Field(ge=0)


class AgentActivity(BaseModel):
    """Active-rate: the share of an agent's alive wall-clock it spent actively
    working versus idle-waiting for its next input. The complement
    (`1 - active_rate`) is how much of its life the agent sat blocked on a
    human/peer message — the metric's reason for being: surface how much an
    agent is gated on people. A project-lead agent that decides/approves every
    minute reads near 100%; one woken once a week reads near 0%; a worker that
    runs flat-out then terminates reads ~100%.

    `active_seconds` = Σ `duration_seconds` of every non-`claim` graph
    `node_exit` in the window — llm generation + execute_code + before/after
    hooks, i.e. all real processing. `llm_seconds` = Σ `duration_seconds` of
    every `turn_end` event within the window — the model's generation wall-clock
    (reasoning + output). `exec_seconds` = Σ `duration_seconds` of every
    `node_exit` for the `exec` node — the code execution wall-clock. The `claim`
    node is excluded because its wall-clock IS the idle-wait (it parks the agent
    in the inbound wait between turns), so it falls into the blocked remainder.
    `alive_seconds` = the agent's alive wall-clock (spawn/resurrect→terminate
    intervals, open tail to now), clipped to the same window — the same lifecycle
    basis as `AgentTps.agent_lifecycle_tps`. `active_rate` = active/alive, capped
    at 1.0 (a node that began before the window's leading edge counts in full, so
    raw active can momentarily exceed windowed alive); 0.0 when alive is 0. All
    fields follow the request's `?hours=` / `?since_compact=` window."""

    model_config = ConfigDict(frozen=True)

    active_seconds: float = Field(ge=0)
    alive_seconds: float = Field(ge=0)
    active_rate: float = Field(ge=0, le=1)
    llm_seconds: float = Field(ge=0)
    exec_seconds: float = Field(ge=0)


class HeartbeatLastPause(BaseModel):
    """The agent's most recent heartbeat pause — when it last opted out of idle
    check-ins and the length it asked for. Sourced from the newest
    `heartbeat_paused` events row; None on AgentInspect when the agent has
    never paused. `at` is when the pause was requested; `duration_s` is the
    requested window in seconds (the agent's `pause_heartbeat(duration)` arg)."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    duration_s: float = Field(ge=0)


class HeartbeatInfo(BaseModel):
    """Idle check-in heartbeat state for one agent — mutually-exclusive display
    states the panel renders:

    - idle-family (idling / restarting — the statuses the fleet view
      projects to "Idle") & not paused & no fresh wake queued: `next_at` is
      set — the daemon's projected check-in due time: the later of
      `last_active_at + idle_threshold + (id mod JITTER_SPAN_S)` and
      `last_heartbeat_at + interval_s` when a prior check-in exists. The daemon
      dispatches the actual check-in at its first poll tick at/after that (at
      most one 15s dispatch step later), so `next_at` is the earliest possible
      check-in, and never later than what the daemon does. An overdue
      projection renders as "due" in the frontend, never as a past time;
      everything else off.
    - idle-family & not paused & a *fresh* wake already queued (created within
      the daemon's 900s `STALE_PENDING_S` freshness window): `heartbeat_pending`
      is True — the daemon suppresses check-ins while a fresh inbound is pending
      (its `NOT EXISTS` guard, windowed by `STALE_PENDING_S`), so no future
      check-in is projected. This is the state a stuck agent sits in (a
      heartbeat check-in it never woke to process); rendering it honestly is
      what stops the panel from projecting a nonsensical past `next_at`. A
      pending inbound older than the window is stale — the daemon checks in on
      the agent anyway, so the panel projects `next_at` instead.
    - paused: `paused_until` is set (the active suppression end); `next_at` None.
    - running or terminated: all off — an active agent never gets a check-in,
      and a dead one never will.

    `interval_s` is the cluster heartbeat interval in seconds (context for the
    `next_at` projection). `last_pause` is the most recent pause from history,
    independent of the current state."""

    model_config = ConfigDict(frozen=True)

    interval_s: int
    next_at: datetime | None = None
    paused_until: datetime | None = None
    heartbeat_pending: bool = False
    last_pause: HeartbeatLastPause | None = None


class AgentInspectLive(BaseModel):
    """GET /api/agents/{id}/inspect/live response — the inspector's cheap,
    window-independent skeleton.

    Every field reflects current database or runner state except
    `heartbeat.last_pause`, which is a single bounded recent-history lookup and
    degrades to None when Loki is unavailable. The response intentionally omits
    cost, stats, TPS, and activity so switching agents does not wait for the
    expensive event-history aggregate fan-out.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: int
    machine: str
    liveness_state: Literal["online", "offline", "unknown"]
    last_probe_at: datetime | None = None
    observation: AgentObservation | None = None
    shells_available: bool | None = None
    spawned_at: datetime
    started_at: datetime | None = None
    shells: list[ShellInfo]
    config_overlay: dict[str, Any]
    notice: OpenNotice | None = None
    heartbeat: HeartbeatInfo


class AgentInspect(BaseModel):
    """GET /api/agents/{id}/inspect response — the per-agent inspector panel.

    A single-agent view, in contrast to `/api/stats/dashboard` (the whole fleet
    aggregated): the agent's live persistent shells, its frozen per-agent config
    overlay, its current open notice (since #152 at most one), its cumulative
    LLM cost, turn/exec stats, active-rate (working vs blocked-on-a-human),
    idle-heartbeat state, and delivery-obligation state.
    `config_overlay` is the spawn-time field-override map (empty when the agent
    runs on cluster defaults). `shells` is probed on the agent's own machine via
    the `shell_probe` cluster op — the gateway never runs sessions itself, so a
    split deployment sees each agent's shells wherever that agent runs.

    `window_hours` echoes the `?hours=` request parameter: None = cumulative
    since spawn (the default), else `cost` + `stats` + `tps` + `activity`
    aggregate only over the past N hours. `applied_window_hours` is the
    actually served window in hours, no greater than `window_hours` and
    clamped to the Loki retention horizon; it is None for whole-life and
    since-compact reads. `since_compact` echoes
    `?since_compact=`: True = those cover only events since the agent's latest
    compact halt (`hours` is ignored and `window_hours` is None). `shells`,
    `config_overlay`, `notice`, and `heartbeat` are always current, independent
    of the window."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    machine: str
    # Gateway-owned liveness projection (Task #1174): 'online' = machine
    # reachable AND (for running/idling) process lease alive; 'offline' =
    # machine unreachable or lease expired; 'unknown' = not yet judged.
    liveness_state: Literal["online", "offline", "unknown"]
    last_probe_at: datetime | None = None
    observation: AgentObservation | None = None
    shells_available: bool | None = None
    spawned_at: datetime
    started_at: datetime | None = None
    window_hours: StatsWindowHours | None = None
    applied_window_hours: int | None = None
    since_compact: bool = False
    shells: list[ShellInfo]
    config_overlay: dict[str, Any]
    notice: OpenNotice | None = None
    cost: AgentCost
    stats: AgentStats
    tps: AgentTps
    activity: AgentActivity
    heartbeat: HeartbeatInfo


class NeighborRow(BaseModel):
    """One row in the GET /api/agents/{id}/neighbors result — a neighbor in
    `neighbors` or an ancestor in `ancestors`."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    status: str
    # Hops from the queried agent: out along ties for `neighbors`, up the
    # spawn chain for `ancestors` (1 = direct).
    depth: int
    # Tie strength, discounted per extra hop. Higher = closer. For ancestors:
    # the lineage edge weight (spawn/fork counts only).
    score: float


class NeighborsResponse(BaseModel):
    """GET /api/agents/{id}/neighbors response."""

    model_config = ConfigDict(frozen=True)

    # Undirected ties ranked by recency-weighted strength, strongest first.
    neighbors: list[NeighborRow]
    # The spawn/fork chain above the queried agent, nearest ancestor first.
    ancestors: list[NeighborRow]
    # True when the frozen-archive read degraded this request (lock-wait skip
    # or failed scan): the tie/lineage set is live-only and must not be read
    # as the complete graph. Defaults False so older clients ignore it.
    degraded: bool = False


class MetricPoint(BaseModel):
    """One sample of a plugin metric's inspector series — the bucket start
    (`ts`) and the aggregated `value` for that bucket. Series come back
    chronologically (the metric templates GROUP BY 1 ORDER BY 1); the frontend
    takes the last point as the "current" value."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    value: float


class PluginMetricResult(BaseModel):
    """One plugin metric rendered for the inspector surface — an element of
    GET /api/agents/{id}/inspect/metrics.

    Mirrors the registered MetricSpec (see `shared/plugin_metrics.py`):
    `panel` selects the payload — `timeseries` / `barchart` metrics carry
    `series` (a bounded recent window, 24h in 1h buckets by default, so at
    most a couple of dozen points), `stat` metrics carry `value` (the single
    aggregate). `error` is set (with `series`/`value` empty) when the metric's
    query failed at execution time — the panel shows the rest of the metrics
    and surfaces the failure inline instead of 500ing the whole request;
    registry-level problems (missing/malformed/tampered file, a template that
    fails the safety re-validation) are HTTP errors instead."""

    model_config = ConfigDict(frozen=True)

    name: str
    title: str
    description: str = ""
    plugin: str = ""
    unit: str = "short"
    panel: Literal["timeseries", "stat", "barchart", "table"]
    error: str | None = None
    value: float | None = None
    series: list[MetricPoint] = Field(default_factory=list)
