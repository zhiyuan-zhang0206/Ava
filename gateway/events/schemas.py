"""Wire models of the event-stream reads: unified and per-agent event rows,
the class-level event-resolution API (task #1468), and the aggregate metrics
reports over the stream.

FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
the wire before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    model_validator,
)

from base.events.contract import EventTier


class AgentEventRow(BaseModel):
    """One row from the unified event stream — admin event log entry (category=telemetry/log).

    `agent_id` is None for service-level lines (gateway / scheduler / labeler
    / runner / etc.) and an int for agent process lines. `payload` keeps the
    full structured record (msg, kwargs from logger.bind / kwargs from the
    call, plus traceback fields when an exception was attached).
    `level` is LOWERCASE (`debug`/`info`/`warning`/`error`/`critical`): the
    W9 switch from the legacy `agent_events` table (loguru's uppercase `INFO`/`WARNING`/
    `ERROR`) flipped the wire values — consumers matching uppercase must
    adapt."""

    model_config = ConfigDict(frozen=True)

    id: int
    line_sha256: str
    ts: datetime
    agent_id: int | None
    level: str
    event: str
    payload: dict[str, Any]


class AgentEventsResponse(BaseModel):
    """GET /api/cluster/admin/events response — newest-first slice."""

    model_config = ConfigDict(frozen=True)

    items: list[AgentEventRow]


class EventRow(BaseModel):
    """One row of the unified event stream.

    Every signal shares this shape (event-system design doc §1): audit
    (legacy `event_log`), telemetry and log (formerly `agent_events`) all land
    in it, written through the unified emitter (`base/telemetry/emitter.py`).
    `trace_id` is the correlation key — one turn = one trace id, every event
    inside it carries the same value. `agent_id` is None for service-level
    events (gateway / daemons); `machine` is the host dimension. `level` is
    LOWERCASE (`debug`/`info`/`warning`/`error`/`critical`) — the retired
    `agent_events` mirror stored loguru's uppercase; the W9 read-path switch
    flipped API consumers to lowercase."""

    model_config = ConfigDict(frozen=True)

    id: int
    line_sha256: str
    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    process: str
    category: str
    event_name: str
    tier: EventTier
    level: str
    source: str
    target_agent_id: int | None
    attributes: dict[str, Any]


class EventsMeta(BaseModel):
    """GET /api/events response header — effective window + pagination
    state. `window_from`/`window_to` echo the applied time window
    (`None` when unbounded high; `window_from` is never `None` — the
    computed `now - hours` when the request used `hours`, the explicit
    `from` when one was given, and otherwise the default lower bound
    `now - 24h`). `has_more` (from the list fetch's +1 lookahead) tells a
    paging client whether another `offset` page exists. `total` is the exact
    filtered row count before paging, computed only when the request asked
    for it (`with_total=1`) — `None` otherwise (it costs a full-window count
    aggregation)."""

    model_config = ConfigDict(frozen=True)

    total: int | None
    window_from: datetime | None
    window_to: datetime | None
    limit: int
    offset: int
    has_more: bool
    generated_at: str  # ISO-8601 UTC


class EventsResponse(BaseModel):
    """GET /api/events response — newest-first slice of the unified event
    stream (`ts DESC`, `id DESC` tiebreak so `limit`/`offset` paging is stable
    across same-`ts` rows)."""

    model_config = ConfigDict(frozen=True)

    meta: EventsMeta
    items: list[EventRow]


EventResolutionCategory = Literal["telemetry", "log"]
EventResolutionLevel = Literal["warning", "error", "critical"]
EventResolutionStatus = Literal["dismissed", "reopened"]


class EventResolutionCreate(BaseModel):
    """One immutable event class to dismiss through the authenticated API.

    An empty ``process`` is a wildcard that dismisses every process of the
    class — the scope every pre-dimension row keeps; a concrete value targets
    one emitting process (task #4329 B5)."""

    model_config = ConfigDict(extra="forbid")

    category: EventResolutionCategory
    level: EventResolutionLevel
    event_name: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=255)
    process: str = Field(default="", max_length=255)
    agent_id: int | None = None
    note: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def _reject_per_agent_v1(self) -> EventResolutionCreate:
        """Keep class arithmetic exact until Loki counts group by agent id."""

        if self.agent_id is not None:
            raise ValueError("agent_id-specific dismissals are not supported in v1")
        return self


class EventResolutionRow(BaseModel):
    """One persisted class dismissal, including reopened history metadata."""

    model_config = ConfigDict(frozen=True)

    id: int
    category: EventResolutionCategory
    level: EventResolutionLevel
    event_name: str
    source: str
    process: str
    agent_id: int | None
    dismissed_by: int
    note: str
    status: EventResolutionStatus
    dismissed_at: datetime
    reopened_at: datetime | None
    burst_count: int | None
    created_at: datetime
    updated_at: datetime


class EventResolutionListResponse(BaseModel):
    """Status-filtered resolution history for the ops agent's review cycle."""

    model_config = ConfigDict(frozen=True)

    resolutions: list[EventResolutionRow]


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
    `cost_usd` prices each call via `base.lm.pricing.cost_usd`; calls on an
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
