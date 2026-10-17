from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import LLM_ERROR_FAMILY, EventSpec, telemetry_event


# meiva:note
# LLM usage events preserve the rates in force at the call and its computed USD cost; readers must not reprice history against the current registry. Calls with unknown prices omit the cost/rate snapshot and set unpriced, while calls is one on every metered call.
# meiva:link base/events/docs/llm-payloads.meiva.okf.md
class LlmUsage(TypedDict):
    model: str
    calls: int
    in_total: int
    out_total: int
    cache_read: NotRequired[int]
    cache_write_5m: NotRequired[int]
    cache_write_1h: NotRequired[int]
    reasoning: int
    latency_ms: float | None
    decode_ms: float | None
    cost_usd: float | None
    price_miss: float | None
    price_hit: float | None
    price_out: float | None
    price_write_5m: NotRequired[float]
    price_write_1h: NotRequired[float]
    unpriced: int | None
    usage_kind: str
    source: NotRequired[str]


class TurnEnd(TypedDict):
    ok: bool
    duration_seconds: float


class SilentIdle(TypedDict):
    output_tokens: int
    cumulative_output_tokens: int
    estimated_cost_usd: float | None
    halted: bool


class LlmRetry(TypedDict):
    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"]
    duration_seconds: float


# meiva:note
# billing is independent of the transient/permanent error class: exhausted credit can require human action in either class. error_type preserves the provider body error.type alone, while error.code may affect billing classification without replacing error_type.
# meiva:link base/events/docs/llm-payloads.meiva.okf.md
class LlmProviderError(TypedDict):
    error_class: str
    provider: str
    status: int | None
    error_type: str | None
    fatal: bool
    billing: bool
    vendor: str | None
    model: str


class CompactionCompleted(TypedDict):
    compact_kind: str
    reminder_shown: bool
    compactions: int
    history_chars: int
    summary_chars: int
    summary_history_ratio: float | None


EVENTS: dict[str, EventSpec] = {
    "llm_usage": telemetry_event("llm_usage", "LLM call metering", payload=LlmUsage, persist=True),
    "turn_end": telemetry_event("turn_end", "one turn finished", payload=TurnEnd, persist=True),
    "llm_turn_aborted": telemetry_event(
        "llm_turn_aborted",
        "turn aborted after retries",
        family=LLM_ERROR_FAMILY,
        tier="anomaly",
        persist=True,
    ),
    "recovery_breaker_halt": telemetry_event(
        "recovery_breaker_halt",
        "recovery circuit breaker tripped — consecutive permanent provider rejections "
        "halted every automatic recovery path until a turn succeeds (task #3617)",
        tier="anomaly",
    ),
    "compact_turn_aborted": telemetry_event(
        "compact_turn_aborted", "turn aborted because compaction failed", tier="anomaly"
    ),
    "llm_provider_error": telemetry_event(
        "llm_provider_error",
        "LLM provider failure",
        payload=LlmProviderError,
        family=LLM_ERROR_FAMILY,
        tier="anomaly",
        persist=True,
    ),
    "thinking_block_sanitized": telemetry_event(
        "thinking_block_sanitized", "thinking block sanitized", tier="noise"
    ),
    "web_fetch_injection_check_failed": telemetry_event(
        "web_fetch_injection_check_failed",
        "the decision-model injection check of a fetched page failed (request error, bad "
        "response or missing key) — the page is returned without a verdict. Carries model "
        "and error_type",
        tier="anomaly",
    ),
    "llm_cancelled": telemetry_event("llm_cancelled", "LLM call cancelled", tier="anomaly"),
    "compaction_completed": telemetry_event(
        "compaction_completed",
        "applied context compaction size reduction and completed count",
        payload=CompactionCompleted,
        tier="noise",
        persist=True,
    ),
    "compact_request": telemetry_event("compact_request", "compact requested", tier="noise"),
    "auto_compact": telemetry_event("auto_compact", "auto-compact", tier="noise", persist=True),
    "compact_reminder": telemetry_event("compact_reminder", "compact reminder", tier="noise"),
    "circuit_breaker_open": telemetry_event(
        "circuit_breaker_open", "heartbeat circuit breaker opened", tier="noise"
    ),
    "circuit_breaker_closed": telemetry_event(
        "circuit_breaker_closed", "heartbeat circuit breaker closed", tier="noise"
    ),
    "heartbeat_circuit_open": telemetry_event(
        "heartbeat_circuit_open", "heartbeat consumed while the breaker is open", tier="noise"
    ),
    "compact_boundary_stamp": telemetry_event(
        "compact_boundary_stamp",
        "compact boundary stamp failed (segment anchor not recorded)",
        tier="noise",
    ),
    "silent_idle": telemetry_event(
        "silent_idle", "silent idle cost-boundary verdict", payload=SilentIdle, tier="noise"
    ),
    "llm_retry": telemetry_event(
        "llm_retry", "LLM retry sequence completion", payload=LlmRetry, tier="observation"
    ),
    "reply_suggestion_gate": telemetry_event(
        "reply_suggestion_gate", "reply suggestion gate probability", tier="noise"
    ),
    "reply_suggestion_gate_failed": telemetry_event(
        "reply_suggestion_gate_failed", "reply suggestion gate call failed", tier="anomaly"
    ),
    "reply_suggestion_draft_failed": telemetry_event(
        "reply_suggestion_draft_failed", "reply suggestion draft call failed", tier="anomaly"
    ),
    "reply_suggestion_draft_rejected": telemetry_event(
        "reply_suggestion_draft_rejected", "reply suggestion draft empty or too long", tier="noise"
    ),
}
