"""Response models for `GET /api/ops/monitor` — the Insights Ops panel.

Shapes mirror `gateway.ops_series_lgtm.fetch_ops_series` output one-for-one
(the router builds the report dict there, then validates it here). Every series
array is positionally aligned with `meta.bucket_starts` via its `bucket`
index; missing buckets are zero-filled by the query core, so a panel can
render the arrays directly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
    event_log_drop: int = Field(ge=0)  # DB log sink shed: queue full


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
