"""Trusted provider-error classification shared by streaming and synchronous calls.

Official LangChain ModelError types own retryability. Raw provider SDK errors
retain their explicit status/transport contracts. Unknown application errors
propagate once; matching attributes or an arbitrary cause do not grant retry
or permanent-provider recovery authority.
"""

from __future__ import annotations

import enum
from typing import Any, NamedTuple, cast

from langchain_core.exceptions import (
    ContextOverflowError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelTimeoutError,
)

from base.log import logger


class ErrorClass(enum.Enum):
    TRANSIENT = "transient"  # retryable in-turn -> falls through to the retry loop
    PERMANENT = "permanent"  # deterministic within the turn -> FatalProviderError, idle
    UNKNOWN = "unknown"  # unrecognized -> propagate once, preserving the original error


# HTTP statuses where retrying the identical request is futile within the turn.
# 400 (bad request / context length / malformed / schema) + 422 (unprocessable)
# are the headline additions over the old 401/402/403-only permanent set: a
# context-overflow 400 used to burn the whole 6-retry (~16-min) budget and then
# kill the agent, when it should fail fast and idle.
_PERMANENT_STATUSES: frozenset[int] = frozenset({400, 401, 402, 403, 404, 422})

# 4xx that are genuinely retryable (429 rate limit, 408 request timeout, 409
# conflict, 425 too-early). 5xx is caught by the `>= 500` range, not this set.
_TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 409, 425, 429})

_CONTEXT_OVERFLOW_STATUS = 400
# Provider wording for "the request exceeds the model's context window". Each
# entry is matched case-folded against the error body's `type` / `code` /
# `message` fields. Sources: anthropic-protocol endpoints (DeepSeek included —
# "This model's maximum context length is N tokens... Please reduce the length
# of the messages or completion"), OpenAI (`context_length_exceeded`,
# "This model's maximum context length is N tokens"), Gemini ("the maximum
# input token limit"), and the OpenAI-compatible CN endpoints (Kimi /
# Moonshot "too many tokens").
_CONTEXT_OVERFLOW_VOCABULARY: frozenset[str] = frozenset(
    entry.lower()
    for entry in (
        "context length",
        "context_length",
        "maximum context",
        "max context",
        "context window",
        "context_window",
        "token limit",
        "too many tokens",
        "prompt is too long",
        "prompt_is_too_long",
        "prompt_too_long",
        "input_too_long",
        "prompt too large",
        "request too large",
        "reduce the length",
    )
)

# "The key is out of credit / its quota is exhausted" — a cross-cutting FACT
# about a failure, not a fourth ErrorClass: it is orthogonal to retryability
# (DeepSeek says it with a PERMANENT 402, OpenAI with a TRANSIENT 429), and it
# is the one failure a human, not a retry, has to clear. Classified here so the
# operator-facing consumers (the `llm_provider_error` billing field, the Grafana
# billing alert) read one predicate instead of each re-deriving a status set.
#
# HTTP 402 Payment Required is the unambiguous signal — DeepSeek returns it for
# `Insufficient Balance`. Providers that reuse a generic 4xx say it in the
# response body instead, in one of two fields, so the vocabulary is matched
# against BOTH `error.type` and `error.code`. Entries keep the vendor's own
# spelling and the match is case-folded on either side, because the vocabularies
# are not consistently cased across vendors. A new provider adds its string here
# and nothing else: the emit site, the event payload and the alert rule all key
# off this one predicate.
#
# Why both fields, and not `code` as a fallback for a missing `type`: DashScope's
# OpenAI-compatible endpoint sends both, with `type` carrying only a broad class
# and `code` the specific reason. Captured 401 (2026-08-20,
# `base/tests/test_qwen_live_smoke.py`)::
#
#     {'error': {'type': 'invalid_request_error',    <- broad class only
#                'code': 'invalid_api_key', ...}}    <- the specific reason
#
# A fallback would therefore never reach `code` on this vendor — `type` is
# present on every 4xx, just useless. Matching both keeps the fields independent,
# and `error.code` is read for this predicate ONLY: it is deliberately not logged
# on the `llm_provider_error` event, so widening the match does not widen what
# the reported `error_type` means for the providers that already say it there.
#
# What is still unverified: every entry below except the DeepSeek 402 path and
# the Google message phrase (both captured live — the latter 2026-09-07, task
# #2610) comes from vendor documentation, not from a captured live 4xx. For
# DashScope matching both fields removes one unknown and leaves one. Removed:
# WHICH field carries an arrears reason no longer decides whether the alert can
# fire. Remaining: the SPELLING. Alibaba documents its arrears codes in a
# single table that never says which entries the OpenAI-compatible endpoint
# re-spells, and its compatible-mode page publishes exactly one code —
# `invalid_api_key`, the body captured above. So the documented PascalCase is
# what goes in; the case-fold absorbs a casing difference, but a different WORD
# on the wire would still miss.
#
# A wrong or missing string costs a MISSED alert, never a false one — but silence
# here is indistinguishable from health: an unmatched string means nothing is
# ever tagged and the rule stays quiet forever, so no incident "surfaces" the gap
# on its own. Two things close it, both operator-driven: the first real arrears
# rejection (capture the raw body and reconcile it with this set), or an opt-in
# live run against a drained key — `base/tests/test_qwen_live_smoke.py` carries
# both instructions. That is why these caveats sit at the definition site rather
# than in a tracker.
_BILLING_STATUS = 402
_BILLING_ERROR_VOCABULARY: frozenset[str] = frozenset(
    entry.lower()
    for entry in (
        "insufficient_quota",  # OpenAI — credit exhausted (arrives as a 429)
        "billing_not_active",  # OpenAI — account not billable
        "insufficient_balance",  # DeepSeek + the OpenAI-compatible CN endpoints
        "exceeded_current_quota_error",  # Moonshot / Kimi
        # Alibaba DashScope (Qwen) — all three from the Model Studio error-code
        # reference, https://www.alibabacloud.com/help/en/model-studio/error-code
        "Arrearage",  # 400 — "make sure your account is in good standing"
        "PrepaidBillOverdue",  # 429 — the prepaid bill is overdue
        "PostpaidBillOverdue",  # 429 — the postpaid bill is overdue
        # Three neighbours on that page are deliberately left out, because this
        # predicate promises "only a payment clears it" and the alert fires on
        # the FIRST occurrence:
        #   `Throttling.AllocationQuota` / `Throttling.RateQuota` (429) — TPS/TPM
        #     rate limiting, not an exhausted account; it would page the operator
        #     during ordinary traffic.
        #   `CommodityNotPurchased` (429) — a model that was never activated: a
        #     setup mistake, not arrears.
        #   `AllocationQuota.FreeTierOnly` (403, free tier exhausted) — reads
        #     like billing, but the page does not say whether a free tier resets
        #     on its own, and a billing alert that can clear without a payment
        #     breaks the contract above. Add it if the reset question is settled.
    )
)

# The one message-only billing vocabulary — Google AI Studio prepaid credits.
# google.genai reports the exhaustion as HTTP 429 with the gRPC status name
# RESOURCE_EXHAUSTED and the reason ONLY in `message` (no `type`/`code` fields
# to match): captured live 2026-09-07 (task #2610) when six gemini workers
# crash-looped on `Your prepayment credits are depleted` with no billing alert
# — the classifier saw no 402, no type, no code. Rate-limit 429s on the same
# API share the status name, so the message phrase — gated on 429 in the
# `billing` predicate — is the discriminator, never the status name alone.
# Unlike the exact-match code vocabulary above, message prose is matched as a
# case-folded SUBSTRING. Official LangChain ModelError wrappers retain their
# typed SDK error as a direct cause; only that contract supplies missing fields.
_BILLING_MESSAGE_VOCABULARY: frozenset[str] = frozenset(
    entry.lower()
    for entry in (
        "prepayment credits are depleted",  # Google — prepaid billing credits exhausted
    )
)


def normalize_provider_transport_error(exc: Exception) -> Exception:
    """Normalize raw HTTP transport only at a model invoke/iteration boundary."""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return ModelTimeoutError(str(exc))
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return ModelConnectionError(str(exc))
    return exc


def _is_transport_error(exc: BaseException) -> bool:
    # SDK imports stay on the failure path rather than loading every provider at boot.
    from anthropic import APIConnectionError as AnthropicConnectionError
    from openai import APIConnectionError as OpenAIConnectionError

    return isinstance(exc, (AnthropicConnectionError, OpenAIConnectionError))


class ErrorClassification(NamedTuple):
    error_class: ErrorClass
    provider: str  # top-level package that raised: anthropic / openai / httpx / builtins / ...
    status: int | None  # HTTP status_code when the SDK carries one, else None
    error_type: str | None  # provider body `error.type` (e.g. engine_overloaded_error), else None
    error_code: str | None  # provider body `error.code` — read by `billing` only, never logged
    error_message: str | None = (
        None  # provider body `error.message` — read by `context_overflow` and the billing message vocabulary
    )

    model_context_overflow: bool = False

    @property
    def billing(self) -> bool:
        """True when the provider refused because the key is out of credit or
        its quota is exhausted (`_BILLING_STATUS` / `_BILLING_ERROR_VOCABULARY`
        / `_BILLING_MESSAGE_VOCABULARY`).

        A derived view, not a field: it is a reading of the same
        (status, error_type, error_code, error_message) the classifier already
        carries, so it cannot drift out of sync with them. The body `type` /
        `code` fields are matched — not `code` as a fallback for an absent
        `type`, which would never reach the vendor that needs it (see the
        vocabulary comment) — and the one provider that says the reason only
        in free-text prose (Google prepaid credits, a 429) is matched by
        phrase against `error_message`, gated on the 429 status so a
        rate-limit rejection quoting the same words cannot fire the
        first-occurrence pager. Deliberately independent of `error_class` —
        an out-of-credit key arrives as a PERMANENT 402 from one vendor and a
        TRANSIENT 429 from another, and the operator has to top the key up
        either way.
        """
        if self.status == _BILLING_STATUS:
            return True
        if any(
            field is not None and field.lower() in _BILLING_ERROR_VOCABULARY
            for field in (self.error_type, self.error_code)
        ):
            return True
        return (
            self.status == 429
            and self.error_message is not None
            and any(v in self.error_message.lower() for v in _BILLING_MESSAGE_VOCABULARY)
        )

    @property
    def context_overflow(self) -> bool:
        """True when the provider rejected the request for exceeding its context
        window — a context-overflow 400 (``"This model's maximum context length
        is N tokens..."`` / ``context_length_exceeded``), the failure shape of
        the 3962 incident.

        Drives the heartbeat circuit breaker: an overflow is the one permanent
        rejection an agent can self-rescue from (compaction shrinks the
        context), unlike auth / billing / schema — so it must be
        distinguishable without scraping the message. A 400 whose body
        carries context-length vocabulary in any of the three body fields
        (`type` / `code` / `message`) counts. One deliberate exception to the
        400 gate: the `context_length_exceeded` TYPE alone counts regardless
        of status — it is the one spelling that says overflow explicitly
        (some providers use it without the generic wording). A 429 carrying
        it is still TRANSIENT (the retry loop retries it; the breaker only
        fires on PERMANENT-class rejections), so the flag is informational
        there — the gate's purpose is only to keep a rate-limit 429 that
        happens to mention "tokens" from being misread as overflow.

        The vocabulary is deliberately conservative (a wrong match routes an
        agent into a compaction, which is harmless; a missed match just means
        the breaker opens with the generic `bad_request` reason and the
        heartbeat stops re-firing — never a wrong destructive action)."""
        if self.model_context_overflow:
            return True
        if self.status != _CONTEXT_OVERFLOW_STATUS and self.error_type != "context_length_exceeded":
            return False
        haystack = " ".join(
            s for s in (self.error_type, self.error_code, self.error_message) if s
        ).lower()
        return any(v in haystack for v in _CONTEXT_OVERFLOW_VOCABULARY)


def _provider_of(exc: BaseException) -> str:
    """Top-level package of the raising exception — the coarse provider label
    (`anthropic` / `openai` / `httpx` / `builtins` / ...) for the postmortem."""
    return type(exc).__module__.split(".", 1)[0]


def _model_error_class(exc: ModelError) -> ErrorClass:
    if exc.is_retryable:
        return ErrorClass.TRANSIENT
    if isinstance(
        exc,
        (
            ModelAuthenticationError,
            ModelPermissionDeniedError,
            ModelInvalidRequestError,
            ModelNotFoundError,
            ContextOverflowError,
        ),
    ):
        return ErrorClass.PERMANENT
    return ErrorClass.UNKNOWN


def _sdk_fields(exc: BaseException) -> tuple[int | None, object]:
    """Read SDK metadata, including one typed cause of an official ModelError."""
    from anthropic import APIError as AnthropicError
    from google.genai.errors import APIError as GoogleError
    from openai import APIError as OpenAIError

    sdk_types = (AnthropicError, OpenAIError, GoogleError)
    candidate: BaseException | None = exc
    if not isinstance(candidate, sdk_types):
        if not isinstance(exc, ModelError) or _model_error_class(exc) is ErrorClass.UNKNOWN:
            return None, None
        candidate = exc.__cause__
    if isinstance(candidate, GoogleError):
        return candidate.code, getattr(candidate, "details", None)
    if isinstance(candidate, (AnthropicError, OpenAIError)):
        status = getattr(candidate, "status_code", None)
        return status if isinstance(status, int) else None, candidate.body
    return None, None


def _error_field(body: object, key: str) -> str | None:
    if not isinstance(body, dict):
        return None
    fields = cast(dict[str, Any], body)
    nested = fields.get("error")
    if isinstance(nested, dict):
        fields = cast(dict[str, Any], nested)
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def classify_error(exc: BaseException) -> ErrorClassification:
    """Classify trusted outer errors; unrecognized wrappers remain UNKNOWN."""
    status, body = _sdk_fields(exc)
    error_class = ErrorClass.UNKNOWN
    if isinstance(exc, ModelError):
        error_class = _model_error_class(exc)
    elif status is not None:
        if status in _PERMANENT_STATUSES:
            error_class = ErrorClass.PERMANENT
        elif status in _TRANSIENT_STATUSES or status >= 500:
            error_class = ErrorClass.TRANSIENT
    elif _is_transport_error(exc):
        error_class = ErrorClass.TRANSIENT
    return ErrorClassification(
        error_class,
        _provider_of(exc),
        status,
        _error_field(body, "type"),
        _error_field(body, "code"),
        _error_field(body, "message"),
        model_context_overflow=isinstance(exc, ContextOverflowError),
    )


def is_retryable_provider_error(exc: BaseException) -> bool:
    """Only a trusted transient provider failure permits another invocation."""
    return classify_error(exc).error_class is ErrorClass.TRANSIENT


def emit_provider_error(
    exc: Exception,
    *,
    model: str,
    fatal: bool,
    classification: ErrorClassification | None = None,
) -> ErrorClassification:
    """Classify and emit one provider failure for streams and synchronous SDK calls.

    The `llm_provider_error` event is the shared source for provider billing
    and rate-limit alerts. Keeping its emission here prevents agent streaming
    and batch SDK paths from silently diverging.
    """
    resolved = classification or classify_error(exc)
    from base.lm.factory import provider_key_of_model

    logger.opt(exception=True).warning(
        "[{label}] {error_class} provider={provider} status={status} fatal={fatal}",
        event="llm_provider_error",
        label="llm_provider_error",
        error_class=resolved.error_class.value,
        provider=resolved.provider,
        status=resolved.status,
        error_type=resolved.error_type,
        fatal=fatal,
        billing=resolved.billing,
        context_overflow=resolved.context_overflow,
        vendor=provider_key_of_model(model),
        model=model,
    )
    return resolved
