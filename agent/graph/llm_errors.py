"""Stream-layer failure taxonomy + consecutive-error tracking for the llm node.

Owns every fail-fast exception the llm node raises (the ``LLMStreamError``
hierarchy, ``FatalLLMStreamError``, ``FatalProviderError``), the provider-error
classification helpers (``shared.lm.errors.classify_error`` → structured log →
optional ``FatalProviderError``), the per-process consecutive-error tracker
that bounds deterministic retry loops, and the stall-pair streak that bounds
the delayed retry schedule for two-adjacent-stall terminations.

Split out of ``_llm.py`` (Task #1004 >800-line outlier) — a leaf dependency of
``_llm_stream`` / ``_llm_cancel`` / ``_llm_chunk``; nothing here imports back
into ``_llm.py``.
"""

from __future__ import annotations

from typing import Any, cast

from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.lm.errors import ErrorClass, classify_error, emit_provider_error


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
    per-attempt duration ceiling). It rides the exception so the
    ``stream_stalled_retry`` event can count stalls by shape without parsing
    the message.
    """

    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(message)
        self.stage = stage


class LLMStreamStallPairError(LLMStreamStallTimeoutError):
    """Two adjacent stalls in one call — the streaming segment stalled and the
    non-streaming fallback then timed out under the same bound.

    Raised by ``_consume_llm`` in place of the fallback's bare ``TimeoutError``
    so the pair is a first-class ``LLMStreamError``: it is retried on the
    delayed stall schedule (``_build._build_llm_retry`` — initial
    ``llm_stall_retry_initial_interval_seconds``, doubling, capped at
    ``llm_stall_retry_max_interval_seconds``, up to
    ``llm_stall_retry_max_consecutive`` consecutive pairs, jittered ±
    ``llm_stall_retry_jitter_fraction``) instead of continuing to burn the
    provider's stalled segments; the whole pair is bounded to ~2x the
    stream-segment bound (one key, one value for both segments).

    Deliberately excluded from the ``_consecutive_errors`` tracker: this
    error's own streak is its bound, and the tracker's cap (default 3) would
    pre-empt the delayed schedule's 4th grant. When the streak exhausts, the
    next attempt raises ``LLMStreamStallPairExhaustedError`` (fatal) at entry.
    """


class FatalLLMStreamError(LLMStreamError):
    """LLM stream retry cap exhausted — same error type N times consecutively;
    fail fast with ERROR event instead of exhausting all retries. A deterministic
    error (e.g. Gemini always timing out at TTFT) can't be fixed by retry.

    Raised by `_check_consecutive_error_cap` when the consecutive-same-error
    count reaches `settings.lm.llm_retry_max_consecutive_same_error`. LangGraph's
    retry policy must NOT retry this — it propagates out of the graph, where
    the agent host emits one ERROR event and re-enters the graph with a fresh
    run: the turn is aborted, the agent stays alive idling for the next
    inbound so the user can see the error and decide the next action.
    """


class LLMRetryBudgetExceededError(FatalLLMStreamError):
    """The LLM node's configurable retry wall-clock budget expired.

    This is excluded from the retry policy, so it reaches the normal turn
    failure path rather than beginning another provider invocation.
    """


class LLMStreamStallPairExhaustedError(FatalLLMStreamError):
    """The delayed stall-retry schedule is spent — the stall pair recurred
    ``llm_stall_retry_max_consecutive`` times consecutively.

    Raised at llm-node entry by ``_check_stall_pair_cap`` instead of burning
    another stalled segment. As a ``FatalLLMStreamError`` it takes the
    established fatal-turn settlement: one ERROR event, the turn aborts, the
    agent stays alive and idles — the regular wake path retries once the
    provider recovers (the wake latency itself is the recovery-wake
    workstream, not this error's).
    """


class FatalProviderError(Exception):
    """Provider permanently rejected the request — a `shared.lm.errors.classify_error`
    `PERMANENT` class (HTTP 400 bad request / context length / schema, 401 auth,
    402 billing, 403 forbidden, 404 unknown model, 422 schema) or a configured
    fatal error type (e.g. `engine_overloaded_error`). Retrying within the turn
    cannot succeed, so — like `FatalLLMStreamError` — it is excluded from the
    retry policy and routed to the agent host's idle path: the turn is aborted but
    the process stays alive idling, so when the underlying cause clears (balance
    topped up, key fixed, context compacted) the next wake-up retries on its own
    rather than needing a manual revive.

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
        # failure the agent can self-rescue from (compaction) — the runloop
        # keys the heartbeat circuit breaker's forced-compact arm on it.
        self.context_overflow = context_overflow


# Provider error types that are deterministic within a turn — retrying cannot flip
# them. Defaults to {"engine_overloaded_error"} (Kimi K3 overload). Configurable
# via settings.lm.llm_fatal_provider_error_types. An empty set disables the check.


def _parse_provider_error_type(exc: BaseException) -> str | None:
    """Extract the error `type` from a provider SDK exception's response body.

    Both OpenAI and Anthropic Python SDKs carry `body` on their API error
    classes (RateLimitError, APIStatusError, etc.), structured as
    ``{"error": {"type": "...", "message": "..."}}``.  This extracts that
    `error.type` string so callers can decide whether it signals a fatal
    condition (e.g. ``"engine_overloaded_error"``) vs a transient one.

    Returns ``None`` when:
    - ``exc`` has no ``body`` attribute, or ``body`` is not a dict
    - ``body["error"]`` is missing or not a dict
    - ``body["error"]["type"]`` is missing or not a string
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = cast(dict[str, Any], body).get("error")
    if not isinstance(error, dict):
        return None
    error_type = cast(dict[str, Any], error).get("type")
    if isinstance(error_type, str) and error_type:
        return error_type
    return None


def _is_fatal_provider_error_type(exc: BaseException) -> bool:
    """Check whether ``exc`` carries a provider error type configured as fatal.

    Returns ``True`` when ``_parse_provider_error_type`` extracts a type
    that is in the configured ``llm_fatal_provider_error_types``
    (comma-separated string parsed into a set). On an empty config string
    the check is a fast no-op.
    """
    fatal_csv = turn_settings.lm.llm_fatal_provider_error_types
    if not fatal_csv:
        return False
    fatal_types = {t.strip() for t in fatal_csv.split(",") if t.strip()}
    error_type = _parse_provider_error_type(exc)
    return error_type is not None and error_type in fatal_types


def _classify_and_log_provider_error(exc: Exception) -> FatalProviderError | None:
    """Classify a provider exception, emit the structured postmortem log, and
    return a `FatalProviderError` to raise when the turn must fail fast — else None
    so the caller re-raises the original for the `RetryPolicy` to retry.

    Two fail-fast triggers fold together here (see `FatalProviderError`): the
    `classify_error` `PERMANENT` class (400/401/402/403/404/422 — deterministic
    rejection) and a configured fatal error *type* (`llm_fatal_provider_error_types`,
    e.g. Kimi K3's transient-but-in-turn-futile `engine_overloaded_error`). The
    structured log fires for every class — including TRANSIENT/UNKNOWN that go on
    to retry — so the `error_class` / `provider` payload keys /
    `->>'status'` are queryable in a postmortem instead of scraping the message.

    `billing` / `vendor` / `model` ride the same log because an out-of-credit key
    is an ops event, not a postmortem detail: the Grafana rule
    `ava-ops-llm-billing-quota` fires on the first `billing=True` row and its IM
    message has to name what stopped working. `vendor` is the model's provider
    key (deepseek / claude / …) — `classification.provider` is only the SDK
    package that raised, which DeepSeek and Claude share. The exception
    message names both, so the runloop's block / recovery notifications
    identify the same account the event does (task #3916).
    """
    classification = classify_error(exc)
    fatal_type_hit = _is_fatal_provider_error_type(exc)
    fatal = classification.error_class is ErrorClass.PERMANENT or fatal_type_hit
    context_overflow = classification.context_overflow
    model = turn_settings.lm.llm_model
    emit_provider_error(exc, model=model, fatal=fatal, classification=classification)
    if not fatal:
        return None
    from shared.lm.factory import provider_key_of_model

    vendor = provider_key_of_model(model)
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

    fail-fast: validate immediately after chunks assemble final_msg; missing
    field raises, langgraph `arun_with_retry` retries the entire llm_node
    (re-streaming, server may not miss this time). Full failure goes through
    the graph crash path.

    The thinking-block drift (#167/#168: signature_delta sent but thinking_delta
    missed) is no longer in this class — `_sanitize_thinking_blocks` repairs it
    in place. A signature-only block filled with `thinking=""` round-trips the
    DeepSeek endpoint (probed 2026-07-25: 200; the key-missing shape 400s with
    "missing field `thinking`"), so the turn keeps its text/tool blocks instead
    of aborting into a doubled re-request.
    """


class LLMStreamSilentIdleError(LLMStreamError):
    """LLM stream produced reasoning (thinking blocks / output tokens) but
    emitted no text content and no tool_call — the turn is a "silent idle"
    where the agent appears stuck at reasoning but never acts.

    No longer raised on the primary detection path: the node now keeps the
    reasoning in context and returns ``halted=False`` so the claim node loops
    straight back to the LLM (no token-wasting blind re-stream), letting the
    ava_silent_idle plugin inject a Continue nudge before the next turn. A
    per-process output-token budget (``_silent_idle_output_tokens``, capped by
    ``settings.lm.llm_silent_idle_max_output_tokens``) bounds a model that
    habitually reasons without acting. Each silent idle consumes at least one
    budget token even when a provider reports zero output tokens; at the cap
    the node halts to idle instead of looping.
    This class is retained as public API for external catch sites.

    The fallthrough case — no reasoning AND no text AND no tool_call — is NOT a
    silent idle; those turns remain a WARNING + halt because the model truly
    produced nothing (no tokens at all), and looping would waste API credits on
    a deterministic empty output.

    Exposed attributes for SQL discrimination:
    - ``output_tokens``: usage_metadata output token count at time of error
    """

    def __init__(
        self,
        message: str,
        *,
        output_tokens: int,
    ) -> None:
        super().__init__(message)
        self.output_tokens = output_tokens


class LLMStreamUnexpectedStopReasonError(LLMStreamError):
    """LLM stream finished but the terminal reason is not in the normal-completion set.

    All non-normal terminal reasons are non-idle abnormal states; framework has
    no explicit handling path — always fail-fast: raise → langgraph
    `arun_with_retry` retries; retry failure goes through turn_end ok=False +
    Error event publish, visible to frontend rather than silent halt.

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


# Tracks the last exception type per thread_id across LangGraph retries.
# If the same type is raised N times consecutively (N = llm_retry_max_consecutive_same_error),
# the next attempt raises FatalLLMStreamError instead -- a deterministic error
# cannot be fixed by retry; fail fast with ERROR event rather than silently
# exhausting all 6 retries over ~16 minutes.
#
# Reset on successful stream completion. Also cleared when the cap fires:
# FatalLLMStreamError aborts only the current turn (the agent host catches it,
# emits one ERROR event, and idles for the next inbound), so the next
# inbound-triggered turn must start with a fresh retry budget rather than
# instantly re-tripping the cap.

_consecutive_errors: dict[str, tuple[str, int]] = {}
"""thread_id -> (exception_type_name, count). Module-level dict; agent process
lifecycle resets it naturally on restart."""


def _check_consecutive_error_cap(thread_id: str) -> None:
    """Raise FatalLLMStreamError if the same error has hit the retry cap.

    Called at the top of _llm_node_impl before the stream starts -- if we
    already know this error is deterministic, skip another 30-480s retry cycle.
    """
    max_cap = settings.lm.llm_retry_max_consecutive_same_error
    if max_cap <= 0:
        return  # Disabled: always exhaust retries
    entry = _consecutive_errors.get(thread_id)
    if entry and entry[1] >= max_cap:
        # Pop before raising: the agent host aborts this turn and keeps the
        # agent alive idling, so the next inbound-triggered turn gets a fresh
        # retry budget instead of instantly re-tripping the cap.
        _consecutive_errors.pop(thread_id)
        raise FatalLLMStreamError(
            f"LLM stream error '{entry[0]}' occurred {entry[1]} times consecutively -- "
            f"retry cap ({max_cap}) exhausted. Deterministic error cannot be fixed by retry; "
            f"failing fast with ERROR event."
        )


def _record_consecutive_error(thread_id: str, exc: BaseException) -> None:
    """Update the consecutive-error tracker after a stream error.

    Only tracks LLMStreamError subclasses; other exceptions are transient
    (network jitter / rate-limit) and should always be retried. Stall pairs
    are excluded: their retry bound is the delayed-schedule streak
    (`_stall_pair_streaks`), and letting this tracker's cap (default 3) count
    them would fail the turn before the schedule's 4th grant.
    """
    if settings.lm.llm_retry_max_consecutive_same_error <= 0:
        return
    if not isinstance(exc, LLMStreamError) or isinstance(exc, LLMStreamStallPairError):
        return
    exc_name = type(exc).__name__
    entry = _consecutive_errors.get(thread_id)
    if entry and entry[0] == exc_name:
        _consecutive_errors[thread_id] = (exc_name, entry[1] + 1)
    else:
        _consecutive_errors[thread_id] = (exc_name, 1)


def _clear_consecutive_errors(thread_id: str) -> None:
    """Reset the consecutive-error tracker on successful stream completion."""
    _consecutive_errors.pop(thread_id, None)


# Consecutive two-adjacent-stall terminations, per thread_id. Incremented when
# `_build._build_llm_retry` grants a delayed retry, reset on a successful
# stream, and popped when the streak exhausts (the next inbound-triggered turn
# must start with a fresh budget rather than instantly re-tripping the fatal
# cap — the same convention as `_consecutive_errors` above).
_stall_pair_streaks: dict[str, int] = {}
"""thread_id -> consecutive stall-pair terminations granted a delayed retry.
Module-level dict; agent process lifecycle resets it naturally on restart."""


def _stall_pair_streak(thread_id: str) -> int:
    """Current streak (0 when the thread has none)."""
    return _stall_pair_streaks.get(thread_id, 0)


def _stall_pair_streak_active(thread_id: str) -> bool:
    """True while a delayed stall-retry sequence is in progress.

    The llm node uses this to keep the transient-retry wall-clock budget
    (`llm_retry_max_total_seconds`, sized for seconds-scale backoffs) from
    ending the delayed schedule's minutes-scale sequence at entry.
    """
    return _stall_pair_streaks.get(thread_id, 0) > 0


def _record_stall_pair_streak(thread_id: str, streak: int) -> None:
    """Store the streak a delayed retry was granted at."""
    _stall_pair_streaks[thread_id] = streak


def _reset_stall_pair_streak(thread_id: str) -> None:
    """Clear the streak (successful stream, or an exhausted sequence)."""
    _stall_pair_streaks.pop(thread_id, None)


def _check_stall_pair_cap(thread_id: str) -> None:
    """Raise the fatal pair error once the delayed schedule is spent.

    Called at the top of `_llm_node_impl` beside `_check_consecutive_error_cap`:
    a streak at the cap means the next attempt would be the (cap+1)-th
    consecutive pair — fail fast into the fatal-turn settlement (one ERROR
    event, agent alive and idling; the regular wake path retries once the
    provider recovers) instead of burning another stalled segment. Pops the
    streak before raising so the next turn starts with a fresh budget.
    """
    max_pairs = settings.lm.llm_stall_retry_max_consecutive
    streak = _stall_pair_streaks.get(thread_id)
    if max_pairs <= 0 or streak is None or streak < max_pairs:
        return
    _stall_pair_streaks.pop(thread_id, None)
    raise LLMStreamStallPairExhaustedError(
        f"LLM stream stall pair recurred {streak} times consecutively — the delayed "
        f"retry budget ({max_pairs} pairs) is exhausted. Aborting the turn; the agent "
        f"stays alive and idles, and the next wake retries once the provider recovers."
    )
