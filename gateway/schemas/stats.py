"""dashboard stats + metrics + token usage.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from datetime import timedelta
from enum import IntEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
)

from shared.loki_index_labels import retention_hours


class StatsWindowHours(IntEnum):
    """Whitelisted `?hours=` windows for stats, inspect, and fleet — 0 = last
    5 minutes / 1h / 6h / 24h / 3d / 7d. An enum (not `int` + range check)
    so FastAPI 422s any other value instead of silently aggregating an arbitrary window.
    (A `Literal[1, 6, ...]` won't do: query params arrive as strings and
    int-literal validation doesn't coerce them, 422ing even valid values.)"""

    M5 = 0
    H1 = 1
    H6 = 6
    H24 = 24
    D3 = 72
    D7 = 168


def window_delta(hours: StatsWindowHours) -> timedelta:
    """Resolve the `?hours=` wire value to its actual aggregation duration."""
    return timedelta(minutes=5) if hours == StatsWindowHours.M5 else timedelta(hours=int(hours))


def applied_window(hours: StatsWindowHours) -> tuple[int, timedelta]:
    """(served hours, duration) — the requested window clamped to the Loki
    retention horizon: rows older than retention are already gone, so the
    window is narrowed instead of silently returning partial data."""
    applied = min(int(hours), retention_hours())
    if applied < int(hours):
        return applied, timedelta(hours=applied)
    return int(hours), window_delta(hours)


class StatsTokens(BaseModel):
    """`/api/stats/dashboard` token sub-section — windowed LLM usage aggregation.

    `cache_hit_pct` = cache_read / input * 100, rounded to 2 decimals;
    input=0 degrades to 0 to avoid div-by-zero."""

    model_config = ConfigDict(frozen=True)

    input: NonNegativeInt
    output: NonNegativeInt
    cache_read: NonNegativeInt
    cache_hit_pct: float = Field(ge=0, le=100)


class StatsDashboard(BaseModel):
    """GET /api/stats/dashboard response — sidebar-top stats card data pulled in one shot.

    All windowed fields (`tokens` / `cost_usd` / `avg_turn_seconds` /
    `warnings` / `errors`) aggregate over the `applied_window_hours` horizon.
    `window_hours` echoes the selected value — `0` means five minutes; all
    other values are hours.
    `applied_window_hours` is the actually served window in hours, no greater
    than `window_hours` and clamped to the Loki retention horizon.

    - `live_count`: current non-terminated count (from agents_meta, not
      events; not windowed)
    - `tokens`: windowed telemetry LLM token usage (cached for at most 60s)
    - `cost_usd`: windowed LLM spend in USD, summed from the usage-time
      `cost_usd` snapshots carried by telemetry Loki `llm_usage` events;
      events that pre-date the snapshot field contribute 0 (cached for at
      most 60s with `tokens`)
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
      (category, level, event_name, source) class has an active dismissal in
      `event_dismissals` count as dismissed, the rest as net, and
      dismissed + net == the raw total by construction.
    - `total_events`: archived event row count (frozen — the PG events copy
      stopped growing at the LGTM cutover; not a live gauge)

    `avg_turn_seconds` None = zero turns in the window (new DB / no
    activity); frontend renders "—"."""

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


class MetricsMeta(BaseModel):
    """`/api/metrics` / `/api/metrics/agents` report header — window +
    provenance. `since_compact` echoes the request's filter: True = each
    agent's events were narrowed to those at or after its latest compact
    halt before aggregating."""

    model_config = ConfigDict(frozen=True)

    window_days: int
    agent_filter: int | None
    generated_at: str  # ISO-8601 UTC; frontend renders in local time
    total_events: NonNegativeInt
    distinct_agents: NonNegativeInt
    since_compact: bool = False


class MetricsReport(BaseModel):
    """GET /api/metrics response — aggregate report over events for the
    settings Metrics tab.

    `metrics` is intentionally a free-form map (one key per registered metric
    unit, e.g. "syntax_fix" / "exec" / "llm_turns" / "agent_activity"). Typing
    each unit's shape here would couple the schema to the metric registry and
    defeat the "add a metric = one function" extensibility, so the frontend
    carries the per-unit data shapes in hand-written TypeScript instead of
    generated types."""

    model_config = ConfigDict(frozen=True)

    meta: MetricsMeta
    metrics: dict[str, Any]


class AgentMetricsItem(BaseModel):
    """One agent's row in the fleet metrics breakdown — headline counters
    aggregated over its events within the report window. `label` is the
    agent's display name (None = unset; frontend falls back to "#id").
    `cost_usd` prices each call via `shared.lm.pricing.cost_usd`; calls on an
    unpriced model contribute 0. `cache_hit_pct` = cached / in * 100 (in=0
    degrades to 0). `exec_failed` is every exec outcome other than plain
    `exec` — same exec-ok/exec-failed split as the metrics report."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    events: NonNegativeInt
    cost_usd: float = Field(ge=0)
    llm_calls: NonNegativeInt
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    tokens_cached: NonNegativeInt
    cache_hit_pct: float = Field(ge=0, le=100)
    turn_ok: NonNegativeInt
    turn_total: NonNegativeInt
    exec_ok: NonNegativeInt
    exec_failed: NonNegativeInt


class AgentMetricsReport(BaseModel):
    """GET /api/metrics/agents response — the per-agent breakdown behind the
    metrics page's Agents panel, sorted by cost descending (ties by agent_id).
    Same window semantics as `/api/metrics`; `meta.since_compact` echoes the
    optional since-last-compact filter."""

    model_config = ConfigDict(frozen=True)

    meta: MetricsMeta
    agents: list[AgentMetricsItem]


class TokenUsageResponse(BaseModel):
    """GET /api/agents/{id}/token-usage response — token usage of the
    most recent LLM call. `input_tokens` is context-window occupancy.

    Real-time deltas rely on the SSE `token_usage` event; when
    switching back to an existing agent, the frontend uses this endpoint
    to fetch the historical last value to initialize the UI (SSE is
    fire-and-forget with no persistence). Returns `(0, 0, 0)` if no
    AIMessage in state.messages carries usage_metadata (new agent /
    has not run any LLM call yet). `reasoning_tokens` is the
    reasoning/thinking portion of output (gemini/openai reasoning
    models + Anthropic thinking); defaults to 0 when absent.

    `soft_compact_tokens` / `hard_compact_tokens` are the agent model's
    wind-down-reminder and force-compact thresholds (a fraction of
    `max_input_tokens`, resolved per agent), so the composer can render the two
    marks against current occupancy without a second request. Both default to 0
    when the agent's model has no known context window.
    """

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    max_input_tokens: int = 0
    soft_compact_tokens: int = 0
    hard_compact_tokens: int = 0


class ContextSection(BaseModel):
    """One node of the system prompt's recursive section breakdown: a heading — or
    the `(preamble)` / `(intro)` prose before the first sub-heading — with its
    (normalized) token estimate. A section over ~1000 tokens is drilled into its
    next-level sub-headings as `children` (recursively); a smaller section, or one
    with no deeper heading, is a leaf (`children == []`). When `children` is
    non-empty their tokens sum to this node's `tokens`."""

    name: str
    tokens: int
    children: list["ContextSection"] = Field(default_factory=list)


class ContextCategory(BaseModel):
    """One context bucket (system_prompt / compact_summary / cluster_memory /
    agent_memory / context_note / user_input / agent_messages / automation /
    reasoning / output / tool_call / tool_response) with its normalized token
    estimate. Inbound messages split by source: user_input (a human turn),
    agent_messages (a peer agent), automation (a machine/framework wakeup)."""

    kind: str
    tokens: int


class ContextBreakdownResponse(BaseModel):
    """GET /api/agents/{id}/context-breakdown — how the agent's context window is
    spent, for the composer's breakdown panel (lazy-loaded when the panel opens).

    Every token value is a chars/4 estimate proportionally normalized to
    `total_input_tokens` (the last LLM call's real input_tokens), so `categories`
    sum exactly to `total_input_tokens` while the distribution stays approximate —
    zero extra API cost, zero KV-cache impact. `sections` break down the
    `system_prompt` category. When no LLM call has run yet (`total_input_tokens ==
    0`) the values are the raw chars/4 estimates and `estimated_total` is their
    sum. `max_input_tokens` / `soft_compact_tokens` / `hard_compact_tokens` mirror
    the token-usage endpoint (0 when the model window is unknown)."""

    total_input_tokens: int
    estimated_total: int
    max_input_tokens: int = 0
    soft_compact_tokens: int = 0
    hard_compact_tokens: int = 0
    sections: list[ContextSection]
    categories: list[ContextCategory]
