"""LLM call, turn, stream, compaction and circuit-breaker events."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from base.events.vocabulary import LLM_ERROR_FAMILY, EventSpec, telemetry_event

# Typed keys are the SQL-injection surface (`payload_keys`): a reader cannot
# reference a key no producer declared.


class LlmUsage(TypedDict):
    """`llm_usage` payload — agent/llm/usage.py:log_llm_usage.

    ``cost_usd`` / ``price_miss`` / ``price_hit`` / ``price_out`` are the
    usage-time price snapshot (user principle: cost is billed incrementally
    with the price in force at the call, never re-priced against the current
    registry). ``cost_usd`` is the call's USD cost at the snapshot rates;
    the three rates are USD per 1M tokens (cache miss / cache hit / output).
    Optional ``price_write_5m`` / ``price_write_1h`` and corresponding
    ``cache_write_5m`` / ``cache_write_1h`` preserve cache-creation subdivisions
    of total input and their TTL-specific USD per 1M rates.
    All four are absent on calls of a model with no known price (a row never
    carries a null cost — absent means unpriced).

    ``calls`` is the constant 1 — it exists so the OTLP mapping mints
    ``ava_llm_usage_calls_total`` (per-agent/per-model call counts come from a
    Counter, not from a histogram's count, which drops the agent_id key).
    ``unpriced`` is 1 exactly when the price snapshot is absent (so unpriced
    call volume is countable in Prometheus); it is omitted on priced calls."""

    model: str
    calls: int
    in_total: int
    out_total: int
    cache_read: int
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
    """`turn_end` payload — agent/graph/llm/node.py."""

    ok: bool
    duration_seconds: float


class SilentIdle(TypedDict):
    """`silent_idle` payload — output-token cost-boundary verdict."""

    output_tokens: int
    cumulative_output_tokens: int
    estimated_cost_usd: float | None
    halted: bool


class LlmRetry(TypedDict):
    """`llm_retry` payload — final duration of a retry sequence."""

    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"]
    duration_seconds: float


class LlmProviderError(TypedDict):
    """`llm_provider_error` payload — base/lm/errors.py.

    One row per classified provider failure — every class, so a postmortem sees
    the retried transients too; ``fatal`` says whether this one aborted the turn.

    ``billing`` is the discriminator the billing/quota alert keys on: True when
    the provider said the key is out of credit or its quota is exhausted (HTTP
    402, or a per-vendor string in the response body's ``error.type`` OR
    ``error.code`` — the vocabulary lives in ``base/lm/errors.py``, so a new
    provider plugs in there and this key and the alert follow with no further
    wiring, wherever the vendor puts the specific reason; that module's comment
    carries the caveats). It is deliberately independent of ``error_class``: one
    vendor says it with a permanent 402, another with a transient 429, and a
    human has to clear it either way.

    ``error_type`` stays the body's ``error.type`` alone. ``error.code`` is read
    for the ``billing`` predicate and not reported here: on the vendors that
    send both, ``type`` is the broad class and ``code`` the specific reason, and
    folding the two into one reported field would change what this key means for
    every provider that already says everything through ``type``.

    ``vendor`` is the model's provider key (deepseek / claude / …, None for an
    unregistered prefix) and ``model`` the model in force at the call — the
    alert names both. ``provider`` is only the SDK package that raised
    (anthropic / openai), which DeepSeek and Claude share, so it cannot answer
    "whose key is dead".
    """

    error_class: str  # transient | permanent | unknown
    provider: str
    status: int | None
    error_type: str | None
    fatal: bool
    billing: bool
    vendor: str | None
    model: str


class CompactionCompleted(TypedDict):
    """`compaction_completed` payload — one applied history replacement."""

    compact_kind: str
    compactions: int
    history_chars: int
    summary_chars: int
    summary_history_ratio: float | None


EVENTS: dict[str, EventSpec] = {
    # turn lifecycle
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
    ),
    # compact / checkpoint / memory housekeeping
    "compact_request": telemetry_event("compact_request", "compact requested", tier="noise"),
    "auto_compact": telemetry_event("auto_compact", "auto-compact", tier="noise", persist=True),
    "compact_reminder": telemetry_event("compact_reminder", "compact reminder", tier="noise"),
    # heartbeat circuit breaker (task #1928)
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
    # reply suggestion (gateway/agents/reply_suggestion)
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
