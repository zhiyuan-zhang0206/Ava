"""Model failure classification and the successful silent-idle token budget."""

from __future__ import annotations

from base.host.env.agent_slices import AgentSlices, LlmCallPolicy
from base.lm.catalog import ModelCatalog
from base.lm.errors import ErrorClass, classify_error, emit_provider_error


class LLMStreamError(Exception):
    """Common parent class for all stream-layer aborts — fail-fast errors that
    `_llm_node_impl` raises before final_msg enters state.messages inherit from here.

    `llm_node`'s except block catches BaseException to write turn_end ok=False +
    traceback + publish Error event so the frontend sees it; SQL queries for
    one class of ops can also use the `exception_type` payload key with `LIKE 'LLMStream%'`
    or distinguish by subclass with more granularity.
    """


class LLMStreamStallTimeoutError(LLMStreamError):
    """LLM stream inter-chunk timeout — no next chunk received within
    the per-stage timeout (TTFT for first chunk, inter-chunk for subsequent);
    abort the current turn to prevent the agent silently hanging when the
    server hangs.

    ``stage`` names WHAT bound expired — ``"ttft"`` (no first chunk),
    ``"mid-stream"`` (gap after at least one chunk) or ``"total"`` (the
    per-attempt duration ceiling). It rides the exception for diagnostics
    without requiring the message to be parsed.
    """

    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(message)
        self.stage = stage


class FatalLLMStreamError(LLMStreamError):
    """An LLM failure that must leave the node without retrying."""


class LLMRetryBudgetExceededError(FatalLLMStreamError):
    """The LLM node's configurable retry wall-clock budget expired.

    This is excluded from the retry policy, so it reaches the normal turn
    failure path rather than beginning another provider invocation.
    """


class FatalProviderError(Exception):
    """Provider permanently rejected the request — a `base.lm.errors.classify_error`
    `PERMANENT` class (HTTP 400 bad request / context length / schema, 401 auth,
    402 billing, 403 forbidden, 404 unknown model, 422 schema) or a configured
    fatal error type (e.g. `engine_overloaded_error`). Retrying within the turn
    cannot succeed, so — like `FatalLLMStreamError` — it is excluded from the
    retry policy and routed to the agent host's idle path. Background wakes
    stop; after correcting the cause, fresh chat or an explicit control can
    request another attempt.

    Carries the structured `(error_class, provider, status)` from the classifier
    so the abort log + Error event are queryable without re-parsing the message.

    Deliberately NOT in the `LLMStreamError` hierarchy: those are mid-stream
    protocol aborts, whereas this is the provider refusing the request outright
    before any stream. So it is not matched by
    the `exception_type` payload key with `LIKE 'LLMStream%'` ops queries.
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: str | None = None,
        provider: str | None = None,
        status: int | None = None,
        context_overflow: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.provider = provider
        self.status = status
        # True when the rejection was the context window being exceeded (the
        # classifier's `context_overflow` predicate). The one permanent
        # failure requiring an explicit compaction; preserve the classification
        # for diagnostics and the suggested operator action.
        self.context_overflow = context_overflow


# Provider error types that are deterministic within a turn — retrying cannot flip
# them. Defaults to {"engine_overloaded_error"} (Kimi K3 overload). Configurable
# via settings.lm.llm_fatal_provider_error_types. An empty set disables the check.


def parse_provider_error_type(exc: BaseException) -> str | None:
    """Read a provider type only through the shared trusted-error classifier."""
    return classify_error(exc).error_type


def is_fatal_provider_error_type(exc: BaseException, llm_policy: LlmCallPolicy) -> bool:
    """Check whether ``exc`` carries a provider error type configured as fatal.

    Returns ``True`` when ``parse_provider_error_type`` extracts a type
    that is in the configured ``llm_fatal_provider_error_types``
    (comma-separated string parsed into a set). On an empty config string
    the check is a fast no-op.
    """
    fatal_csv = llm_policy.llm_fatal_provider_error_types
    if not fatal_csv:
        return False
    fatal_types = {t.strip() for t in fatal_csv.split(",") if t.strip()}
    error_type = parse_provider_error_type(exc)
    return error_type is not None and error_type in fatal_types


def classify_and_log_provider_error(
    exc: Exception, agent: AgentSlices, *, catalog: ModelCatalog
) -> FatalProviderError | None:
    """Classify a provider exception, emit the structured postmortem log, and
    return a `FatalProviderError` to raise when the turn must fail fast — else None
    so the caller re-raises the original for the node's explicit retry predicate.

    Two fail-fast triggers fold together here (see `FatalProviderError`): the
    `classify_error` `PERMANENT` class (400/401/402/403/404/422 — deterministic
    rejection) and a configured fatal error *type* (`llm_fatal_provider_error_types`,
    e.g. Kimi K3's transient-but-in-turn-futile `engine_overloaded_error`). The
    structured log fires for every class, including UNKNOWN errors that propagate
    once, so the `error_class` / `provider` payload keys /
    `->>'status'` are queryable in a postmortem instead of scraping the message.

    `billing` / `vendor` / `model` ride the same log because an out-of-credit key
    is an ops event, not a postmortem detail: the Grafana rule
    `meiva-ops-llm-billing-quota` fires on the first `billing=True` row and its IM
    message has to name what stopped working. `vendor` is the model's provider
    key (deepseek / claude / …) — `classification.provider` is only the SDK
    package that raised, which DeepSeek and Claude share. The exception
    message names both, so the runloop's block / recovery notifications
    identify the same account the event does (task #3916).
    """
    classification = classify_error(exc)
    fatal_type_hit = is_fatal_provider_error_type(exc, agent.llm_policy)
    fatal = classification.error_class is ErrorClass.PERMANENT or fatal_type_hit
    context_overflow = classification.context_overflow
    model = agent.brain.llm_model
    emit_provider_error(
        exc, model=model, fatal=fatal, classification=classification, catalog=catalog
    )
    if not fatal:
        return None
    from base.lm.factory import provider_key_of_model

    vendor = provider_key_of_model(model, catalog=catalog)
    if classification.billing:
        reason = "provider rejected the request for billing: the key is out of credit or quota"
    elif classification.error_class is ErrorClass.PERMANENT:
        reason = "provider permanently rejected the request"
    else:
        reason = f"provider returned configured-fatal error type {classification.error_type!r}"
    return FatalProviderError(
        f"{reason} (HTTP {classification.status}, vendor={vendor}, "
        f"provider={classification.provider}): {exc}. "
        "Retry cannot succeed; aborting the turn — the agent stays alive and idles.",
        error_class=classification.error_class.value,
        provider=classification.provider,
        status=classification.status,
        context_overflow=context_overflow,
    )


class LLMStreamCorruptedError(LLMStreamError):
    """LLM stream accumulated to final_msg but with incomplete protocol fields (upstream drift).

    Covers terminal-frame drift:
    - terminal reason missing from response_metadata — validator cannot determine
      whether the model stopped the turn normally; fallthrough must not treat
      it as idle.
    - terminal reason claims a tool call but no tool_calls survived (bug 169).

    Validate immediately after chunks assemble final_msg. Missing fields raise
    once through the graph failure path without automatically re-streaming.

    The thinking-block drift (#167/#168: signature_delta sent but thinking_delta
    missed) is no longer in this class — `_sanitize_thinking_blocks` repairs it
    in place. A signature-only block filled with `thinking=""` round-trips the
    DeepSeek endpoint (probed 2026-07-25: 200; the key-missing shape 400s with
    "missing field `thinking`"), so the turn keeps its text/tool blocks instead
    of aborting into a doubled re-request.
    """


class LLMStreamUnexpectedStopReasonError(LLMStreamError):
    """LLM stream finished but the terminal reason is not in the normal-completion set.

    All non-normal terminal reasons are non-idle abnormal states. With no
    continuation contract, raise once through turn_end ok=False and Error event
    publication; do not automatically re-stream the request.

    Known non-normal terminal reasons (across providers):
    - truncation: server output budget exhausted. See subclass `LLMStreamTruncatedError`.
    - safety / content_filter / recitation: model declined; should surface to user.
    - provider-specific abnormal values (stop_sequence, pause_turn, etc.): raise
      to surface; framework has no continuation protocol for them.

    Catching the parent class catches all "abnormal stop turns"; subclass for
    distinguishing truncation.

    `stop_reason` is exposed as an attribute via `__init__` (not just stuffed
    in f-string), so the catch site can programmatically dispatch without
    regex-parsing the message.
    """

    def __init__(
        self,
        message: str,
        *,
        stop_reason: str,
        output_tokens: int,
    ) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
        self.output_tokens = output_tokens


class LLMStreamTruncatedError(LLMStreamUnexpectedStopReasonError):
    """LLM stream truncated by the server's output budget (anthropic
    `stop_reason='max_tokens'` / openai `finish_reason='length'` / gemini
    `finish_reason='MAX_TOKENS'`; the raw provider value is on `self.stop_reason`).

    Subclassed separately because max_tokens truncation has a deterministic
    client-side fix (bump `max_tokens`), unlike `refusal` / `pause_turn`
    which have no known retry path. SQL
    `WHERE exception_type = 'LLMStreamTruncatedError'` pulls all turns hit
    by client-side cap being too small at a glance.

    This invariant was formed by a max_tokens truncation incident (agent 169).
    """


class LlmLedger:
    """Output-token budget of consecutive successful silent-idle turns.

    One ledger belongs to one graph; records are keyed by agent thread id.
    Provider failures have no cross-turn retry state.
    """

    def __init__(self) -> None:
        self._silent_idle_output_tokens: dict[str, int] = {}

    def silent_idle_output_tokens(self, thread_id: str) -> int:
        """Budget units the thread's consecutive silent-idle turns have used so far."""
        return self._silent_idle_output_tokens.get(thread_id, 0)

    def record_silent_idle_output_tokens(self, thread_id: str, cumulative: int) -> None:
        """Store the cumulative budget after one more silent-idle turn."""
        self._silent_idle_output_tokens[thread_id] = cumulative

    def reset_silent_idle(self, thread_id: str) -> None:
        """End the silent-idle streak (a real action, or the cap firing)."""
        self._silent_idle_output_tokens.pop(thread_id, None)
