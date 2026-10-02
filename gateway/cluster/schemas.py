"""Wire models of the cluster surface: system + cluster status and the
machine pause/resume/delete admin responses, the stats-dashboard aggregate, and
the `GET /api/ops/monitor` series (the Insights Ops panel).

`MachineStatus` (the roster row the CLI also decodes) lives in
`base.api_contracts.status` so `cli` can decode the roster without importing
up into `gateway`; the models below are the gateway-only status surface.

The ops-monitor shapes mirror `gateway.cluster.ops_series_lgtm.fetch_ops_series`
output one-for-one (the router builds the report dict there, then validates it
here). Every series array is positionally aligned with `meta.bucket_starts` via
its `bucket` index; missing buckets are zero-filled by the query core, so a
panel can render the arrays directly.

FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
the wire before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
)

from base.api_contracts.status import MachineStatus
from gateway.schemas.stats import StatsWindowHours


class ServiceItem(BaseModel):
    """Online status of a single daemon."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    online: bool | None  # None = unknown (cannot probe)
    pid: int | None
    detail: str | None


class ServicesStatus(BaseModel):
    """GET /api/status services sub-section."""

    model_config = ConfigDict(frozen=True)

    items: list[ServiceItem]


class ClusterPanel(BaseModel):
    """GET /api/status cluster sub-section — multi-machine view.

    `current_*` is the perspective of the gateway that received this
    request; `machines` is every machine registered in DB + live probe
    results.
    """

    model_config = ConfigDict(frozen=True)

    current_machine: str
    # This host's three capability flags (any combination on a single-box host).
    # current_serve_observability_station defaults False so pre-station clients
    # parse the payload.
    current_serve_gateway: bool
    current_serve_agent_runner: bool
    current_serve_observability_station: bool = False
    current_paused: bool  # whether the current gateway is paused (local is_paused())
    machines: list[MachineStatus]


class SystemStatus(BaseModel):
    """GET /api/status response — System Status panel data all in one go."""

    model_config = ConfigDict(frozen=True)

    services: ServicesStatus
    cluster: ClusterPanel


class AgentMachineRow(BaseModel):
    """One machine as exposed to agents via ava.agents.list_machines().

    Intentionally minimal: name + free-text description + determinate liveness.
    A paused host is still live, but a reached host whose status operation
    failed has no liveness verdict, so `live=False` until a probe returns a
    concrete paused value. role / gateway_url stay internal to ops and are not
    surfaced to agents.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None = None
    live: bool
    # Operator-set staging flag — agents see it so a peer can tell a staging
    # host from a production rollout target (e.g. before enrolling against it).
    is_staging: bool = False


class MachineDeleteResponse(BaseModel):
    """DELETE /api/cluster/machines/{name} response."""

    model_config = ConfigDict(frozen=True)

    deleted: bool  # True = row existed and was removed; False = already absent


class MachinePauseRequest(BaseModel):
    """POST /api/cluster/machines/{name}/pause body.

    `reason` is free-text why the machine is being pulled out (e.g. "a week off")
    — recorded on the machines row as `pause_reason` for the resume checklist.
    """

    reason: str = ""


class MachinePauseResponse(BaseModel):
    """POST /api/cluster/machines/{name}/pause response — what the pause did.

    The pause is the three-step operator act: drain (reassign in_progress
    tasks owned by the machine's agents to the drain owner), terminate every
    live agent on the machine (graceful via its ops server; agents whose
    graceful terminate could not be enqueued — machine already unreachable —
    are force-marked terminated in the shared DB), then set the pause latch.
    `paused_at`/`pause_reason` are the row values after the latch write.
    Idempotent: pausing an already-paused machine terminates nothing (its
    agents are already terminated) and returns the existing latch values.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    paused: bool  # True = the machine now carries the pause latch
    terminated_agents: int  # killed + marked terminated via the machine's ops server
    force_marked_agents: int  # ops unreachable; row force-marked terminated in the DB
    reassigned_tasks: int  # in_progress tasks drained to the drain owner
    paused_at: datetime | None = None
    pause_reason: str | None = None


class MachineResumeResponse(BaseModel):
    """POST /api/cluster/machines/{name}/resume response.

    `resumed` True = the pause latch was cleared (the machine is a normal
    cluster member again); False = it was not paused (idempotent no-op).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    resumed: bool


class StatsTokens(BaseModel):
    """`/api/stats/dashboard` token sub-section — windowed LLM usage aggregation.

    `cache_hit_pct` = cache_read / input * 100, rounded to 2 decimals;
    input=0 degrades to 0 to avoid div-by-zero."""

    model_config = ConfigDict(frozen=True)

    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt
    cache_hit_pct: float = Field(ge=0, le=100)


# The plugin-stats status vocabulary, mirrored from `base.packages.plugins.stats`
# (the DB-side writer + check constraint). There is deliberately no "empty":
# a card with no row IS the frontend's empty state, and a second spelling of
# that would be a second fact to keep in sync.
PluginStatStatus = Literal["ok", "warn", "error"]


class PluginStat(BaseModel):
    """One declared statistics-panel card's current value (task #2911).

    The declaration half (existence, label) arrives via
    `GET /api/ui/contributions` (`UiStatContribution`); this is the runtime
    half, keyed by `(plugin, id)` and joined against the declaration by the
    console. Values are NOT windowed: the window selector governs the
    console's own aggregates, while a usage meter's "current" is a
    point-in-time fact and would be meaningless averaged over a horizon.

    `updated_at` is when the plugin last wrote the row — the console renders
    its age, so a refresh that stopped running cannot pass for a fresh value.
    """

    model_config = ConfigDict(frozen=True)

    plugin: str
    id: str
    value: str
    detail: str | None
    status: PluginStatStatus
    updated_at: datetime
    updated_by: str | None


class StatsDashboard(BaseModel):
    """GET /api/stats/dashboard response — sidebar-top stats card data pulled in one shot.

    All windowed fields (`tokens` / `cost_usd` / `avg_turn_seconds` /
    `warnings` / `errors`) aggregate over the `applied_window_hours` horizon.
    `window_hours` echoes the selected value — `0` means five minutes; all
    other values are hours.
    `applied_window_hours` is the served window in hours: the requested one.

    - `live_count`: current non-terminated count (from agents_meta, not
      events; not windowed)
    - `tokens`: windowed telemetry LLM token usage
    - `cost_usd`: windowed LLM spend in USD, summed from the usage-time
      `cost_usd` snapshots carried by `llm_usage` events in `telemetry_events`;
      events that pre-date the snapshot field contribute 0
    - `avg_turn_seconds`: windowed avg LLM call wall time
      (event=turn_end + ok=true)
    - `warnings` / `errors`: raw level totals over the window (critical
      folds into error). Agent trial-and-error (exec_failed) logs at INFO
      and is deliberately NOT counted — these numbers are operator-facing
      alerts, not agent activity.
    - `warnings_dismissed` / `warnings_net` / `errors_dismissed` /
      `errors_net`: the three-way resolution split (task #1935). The
      arithmetic is the events-maintenance daemon's class subtraction
      (`services.events_maintenance.resolution.level_splits`) applied to the
      SELECTED window instead of the daemon's fixed six hours: events whose
      (category, level, event_name, source, process) class has an active
      dismissal in `event_dismissals` — exact, or matching a wildcard row
      with an empty `process` — count as dismissed, the rest as net, and
      dismissed + net == the raw total by construction.
    - `total_events`: archived event row count (frozen — the PG events copy
      stopped growing at the LGTM cutover; not a live gauge)

    `avg_turn_seconds` None = zero turns in the window (new DB / no
    activity); frontend renders "—".

    `plugin_stats` is not windowed (see `PluginStat`): the runtime values
    behind cards that plugins declare under `contributions.ui.stats`, joined
    by the console on `(plugin, id)`.

    `as_of` is the UTC time the payload's reads were assembled."""

    model_config = ConfigDict(frozen=True)

    live_count: NonNegativeInt
    window_hours: StatsWindowHours
    applied_window_hours: int | None = None
    tokens: StatsTokens
    cost_usd: float = Field(ge=0)
    avg_turn_seconds: float | None
    warnings: NonNegativeInt
    errors: NonNegativeInt
    warnings_dismissed: NonNegativeInt
    warnings_net: NonNegativeInt
    errors_dismissed: NonNegativeInt
    errors_net: NonNegativeInt
    total_events: NonNegativeInt
    plugin_stats: list[PluginStat]
    as_of: datetime | None = None


# NonNegativeInt / None-able percentile fields


class OpsMonitorMeta(BaseModel):
    """Window + provenance of one `/api/ops/monitor` response."""

    model_config = ConfigDict(frozen=True)

    window: str  # "1h" | "6h" | "24h" | "7d"
    bucket_seconds: int = Field(ge=1)
    generated_at: str  # ISO-8601 UTC
    bucket_starts: list[str]  # ISO-8601 UTC, oldest first, aligned to date_bin origin


class SseDropBucket(BaseModel):
    """One bucket's SSE/event-log backlog footprint — dropped records by cause."""

    model_config = ConfigDict(frozen=True)

    bucket: int
    queue_full: int = Field(ge=0)  # agent SSE publisher shed: local queue full
    publish_error: int = Field(ge=0)  # agent SSE publisher shed: redis publish failed/slow
    event_log_drop: int = Field(ge=0)  # log sink shed: queue full


class SseTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    queue_full: int = Field(ge=0)
    publish_error: int = Field(ge=0)
    event_log_drop: int = Field(ge=0)


class SseReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: list[SseDropBucket]
    totals: SseTotals


class LlmBucket(BaseModel):
    """One bucket's LLM call profile. Latency percentiles are over rows that
    carry `latency_ms` (pre-instrumentation rows are NULL and ignored);
    `tps` = Σ(in+out+reasoning) tokens / Σ latency seconds (NULL when no
    latency data). `errors` counts llm_provider_error / stream_stalled_retry /
    llm_turn_aborted events."""

    model_config = ConfigDict(frozen=True)

    bucket: int
    calls: int = Field(ge=0)
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_max_ms: float | None
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    tps: float | None
    errors: int = Field(ge=0)


class LlmTotals(BaseModel):
    """Whole-window LLM profile — same fields as one LlmBucket, no bucket."""

    model_config = ConfigDict(frozen=True)

    calls: int = Field(ge=0)
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_max_ms: float | None
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    tps: float | None
    errors: int = Field(ge=0)


class LlmReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: list[LlmBucket]
    totals: LlmTotals


class RestartBucket(BaseModel):
    """One bucket's process-restart counts: agent processes (agent_restarted)
    and gateway-side services (service_started)."""

    model_config = ConfigDict(frozen=True)

    bucket: int
    agent_restarts: int = Field(ge=0)
    service_starts: int = Field(ge=0)


class ServiceRestartRow(BaseModel):
    """One service's boot count within the window — `name` is the daemon
    identity passed to `init_gateway_process` (gateway / agent-host / watchdog /
    delivery_watchdog / labeler / memory_indexer / heartbeat / ...)."""

    model_config = ConfigDict(frozen=True)

    name: str
    starts: int = Field(ge=0)
    last_start: str | None  # ISO-8601 UTC


class AgentRestartRow(BaseModel):
    """One agent's restart count within the window."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    restarts: int = Field(ge=0)


class RestartTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_restarts: int = Field(ge=0)
    service_starts: int = Field(ge=0)


class RestartReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    series: list[RestartBucket]
    services: list[ServiceRestartRow]
    agents: list[AgentRestartRow]
    totals: RestartTotals


class OpsMonitorReport(BaseModel):
    """GET /api/ops/monitor response — the whole panel in one round trip."""

    model_config = ConfigDict(frozen=True)

    meta: OpsMonitorMeta
    sse: SseReport
    llm: LlmReport
    restarts: RestartReport
