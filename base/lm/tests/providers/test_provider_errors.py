"""Unit tests for base/lm/errors.py:classify_error — the cross-provider
exception taxonomy (transient / permanent / unknown). Sibling to
test_provider_stop.py, which covers the terminal-reason classifier.
"""

from collections.abc import Mapping

import anthropic
import httpx
import httpx2
import openai
import pytest
from google.genai.errors import APIError as GoogleAPIError
from google.genai.errors import ClientError, ServerError
from langchain_core.exceptions import ModelError
from langchain_google_genai.chat_models import (
    ChatGoogleGenerativeAIError,
    _handle_client_error,
    _handle_server_error,
)

from base.lm.errors import ErrorClass, classify_error


def _sdk_status_error(status: int, body: object = None) -> openai.APIStatusError:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://audit.invalid"))
    return openai.APIStatusError(f"HTTP {status}", response=response, body=body)


def _genai_error(code: int, details: Mapping[str, object] | None = None) -> GoogleAPIError:
    error_type = ServerError if code >= 500 else ClientError
    return error_type(code, dict(details or {}))


def _google_wrapper(message: str, cause: GoogleAPIError | None) -> Exception:
    if cause is None:
        return ChatGoogleGenerativeAIError(message)
    if not isinstance(cause, (ServerError, ClientError)):
        raise TypeError("test data must construct a concrete SDK error")
    try:
        if isinstance(cause, ServerError):
            _handle_server_error(cause)
        else:
            _handle_client_error(cause, {"model": "gemini-3.7-flash"})
    except Exception as error:
        return error
    raise AssertionError("official handler must raise")


# ───────────── permanent statuses ─────────────


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_permanent_statuses(status: int) -> None:
    """400 (bad request / context length / schema), 401 auth, 402 billing, 403
    forbidden, 404 unknown model, 422 schema — deterministic within the turn."""
    result = classify_error(_sdk_status_error(status))
    assert result.error_class is ErrorClass.PERMANENT
    assert result.status == status


def test_context_length_400_is_permanent_not_retried() -> None:
    """The headline gap this taxonomy closes: a context-overflow 400 is PERMANENT,
    so the node fails fast + idles instead of burning the full retry budget and
    dying. The classifier does not need the message text — the status is enough."""
    exc = _sdk_status_error(
        400, {"error": {"type": "invalid_request_error", "message": "prompt is too long"}}
    )
    result = classify_error(exc)
    assert result.error_class is ErrorClass.PERMANENT
    assert result.error_type == "invalid_request_error"


# ───────────── transient statuses ─────────────


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504, 529])
def test_transient_statuses(status: int) -> None:
    """429 rate limit, 5xx server (>= 500 range), 408/409/425 — retryable in-turn."""
    result = classify_error(_sdk_status_error(status))
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.status == status


# ───────────── unknown statuses ─────────────


@pytest.mark.parametrize("status", [411, 413, 415, 418])
def test_unrecognized_status_is_unknown_not_guessed(status: int) -> None:
    """A 4xx we don't explicitly place (413 payload-too-large etc.) is UNKNOWN,
    never guessed into permanent or transient — it propagates through the normal
    failure path once without a guessed retry."""
    result = classify_error(_sdk_status_error(status))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.status == status


# ───────────── transport (no status) → transient ─────────────


def test_transport_errors_are_transient() -> None:
    """Connection / timeout failures carry no HTTP status; the SDKs' own base
    types classify them TRANSIENT; raw application transport errors remain unknown."""
    req = httpx2.Request("POST", "http://x")
    transport_excs: list[BaseException] = [
        anthropic.APIConnectionError(request=req),
        anthropic.APITimeoutError(request=req),  # subclass of APIConnectionError
        openai.APIConnectionError(request=req),
        openai.APITimeoutError(request=req),
    ]
    for exc in transport_excs:
        result = classify_error(exc)
        assert result.error_class is ErrorClass.TRANSIENT, exc
        assert result.status is None


# ───────────── unknown (non-status, non-transport) ─────────────


def test_unrecognized_exception_is_unknown() -> None:
    """An exception with neither a status_code nor a known transport type is
    UNKNOWN — the fail-fast fallthrough, never silently treated as transient."""
    result = classify_error(ValueError("something we've never seen"))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.status is None


# ───────────── status_code / body edge cases ─────────────


def test_non_int_status_code_falls_through() -> None:
    """A wrapper that exposes a non-int status_code (e.g. the string '429') is not
    trusted as a status; it falls through to transport / UNKNOWN — no worse."""

    class UnrelatedError(Exception):
        status_code = "429"

    result = classify_error(UnrelatedError())
    assert result.status is None
    assert result.error_class is ErrorClass.UNKNOWN


def test_error_type_extracted_from_body() -> None:
    result = classify_error(
        _sdk_status_error(429, {"error": {"type": "engine_overloaded_error", "message": "busy"}})
    )
    assert result.error_type == "engine_overloaded_error"
    assert result.error_class is ErrorClass.TRANSIENT  # 429 nature is transient


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not-a-dict",
        {"no_error_key": 1},
        {"error": "not-a-dict"},
        {"error": {"no_type": 1}},
        {"error": {"type": ""}},
    ],
)
def test_error_type_none_on_malformed_body(body: object) -> None:
    """A missing / malformed body yields error_type=None without affecting the
    status-driven class."""
    result = classify_error(_sdk_status_error(500, body))
    assert result.error_type is None
    assert result.error_class is ErrorClass.TRANSIENT


# ───────────── provider label ─────────────


def test_provider_label_from_module() -> None:
    """provider is the top-level package that raised — the coarse postmortem label."""
    req = httpx2.Request("POST", "http://x")
    assert classify_error(anthropic.APIConnectionError(request=req)).provider == "anthropic"
    assert classify_error(openai.APIConnectionError(request=req)).provider == "openai"
    assert classify_error(httpx.ReadError("x")).provider == "httpx"
    assert classify_error(ValueError("x")).provider == "builtins"


# ───────────── billing / quota exhaustion ─────────────


def test_402_is_billing() -> None:
    """HTTP 402 Payment Required is the unambiguous cross-provider signal
    (DeepSeek returns it for `Insufficient Balance`)."""
    result = classify_error(_sdk_status_error(402))
    assert result.billing is True
    assert result.error_class is ErrorClass.PERMANENT


@pytest.mark.parametrize(
    "error_type",
    [
        "insufficient_quota",
        "billing_not_active",
        "insufficient_balance",
        "exceeded_current_quota_error",
        "arrearage",
        "Arrearage",  # DashScope cases its codes; the match is case-folded
    ],
)
def test_billing_error_types_are_billing(error_type: str) -> None:
    """Providers that reuse a generic 4xx say it in the body's error.type
    instead — matched by vocabulary so a vendor plugs in at one place."""
    exc = _sdk_status_error(429, {"error": {"type": error_type, "message": "no balance"}})
    assert classify_error(exc).billing is True


@pytest.mark.parametrize(
    "error_code",
    [
        "Arrearage",  # DashScope 400 — the account is in arrears
        "PrepaidBillOverdue",  # DashScope 429 — subscription bill overdue
        "PostpaidBillOverdue",
        "arrearage",  # the vendor's casing is not load-bearing; the match folds
    ],
)
def test_dashscope_billing_codes_are_billing(error_code: str) -> None:
    """DashScope's OpenAI-compatible endpoint reports the broad class in
    `error.type` and the SPECIFIC reason in `error.code` — that SHAPE is
    captured (2026-08-20, `base/tests/test_qwen_live_smoke.py`), so the
    vocabulary is matched against both fields.

    The body here is synthetic: the captured one is an auth failure, and no live
    arrears body has been seen. What is asserted is the shape plus Alibaba's
    documented arrears codes, which is what the vocabulary claims.

    A `code`-when-`type`-is-missing fallback would be no fix at all here: `type`
    is present on every DashScope 4xx, just too coarse to carry a reason.
    """
    exc = _sdk_status_error(
        400,
        {"error": {"type": "invalid_request_error", "code": error_code, "message": "no balance"}},
    )
    assert classify_error(exc).billing is True


def test_billing_code_match_does_not_widen_the_reported_error_type() -> None:
    """Widening the MATCH must not widen the reported field: `error_type` on the
    `llm_provider_error` event stays the body's `error.type` alone, so its
    meaning is unchanged for the providers that already say everything there."""
    exc = _sdk_status_error(
        400, {"error": {"type": "invalid_request_error", "code": "Arrearage", "message": "x"}}
    )
    result = classify_error(exc)
    assert result.billing is True
    assert result.error_type == "invalid_request_error"


# ───────────── context overflow (the circuit-breaker trigger) ─────────────


def test_context_overflow_400_with_length_message() -> None:
    """The 3962 failure shape: a 400 whose message says the context length was
    exceeded is flagged context_overflow — the heartbeat circuit breaker's
    self-rescue trigger."""
    exc = _sdk_status_error(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "This model's maximum context length is 1048576 tokens. "
                    "However, you requested 1076056 tokens (692056 in the "
                    "messages, 384000 in the completion). Please reduce the "
                    "length of the messages or completion."
                ),
            }
        },
    )
    result = classify_error(exc)
    assert result.error_class is ErrorClass.PERMANENT
    assert result.context_overflow is True


@pytest.mark.parametrize(
    "error_type",
    ["context_length_exceeded", "prompt_is_too_long", "prompt_too_long"],
)
def test_context_overflow_error_type_vocabulary(error_type: str) -> None:
    """OpenAI-style `context_length_exceeded` (and the type-level vocabulary
    spellings) flag overflow from the error type alone — no message needed."""
    exc = _sdk_status_error(400, {"error": {"type": error_type}})
    assert classify_error(exc).context_overflow is True


def test_schema_400_is_not_context_overflow() -> None:
    """A plain bad-request 400 (schema / malformed) carries none of the
    context-length vocabulary — it must NOT be flagged overflow."""
    exc = _sdk_status_error(
        400, {"error": {"type": "invalid_request_error", "message": "bad schema"}}
    )
    result = classify_error(exc)
    assert result.error_class is ErrorClass.PERMANENT
    assert result.context_overflow is False


def test_rate_limit_429_mentioning_tokens_is_not_overflow() -> None:
    """A rate-limit 429 that happens to mention tokens must not be misread as
    overflow — the predicate is status-gated to 400."""
    exc = _sdk_status_error(
        429, {"error": {"type": "rate_limit_error", "message": "too many tokens per minute"}}
    )
    assert classify_error(exc).context_overflow is False


def test_429_with_context_length_exceeded_type_stays_transient() -> None:
    """The `context_length_exceeded` type exception to the status gate is
    informational only: a 429 carrying that type is still TRANSIENT (the
    retry loop retries it — it never opens the breaker, which only fires on
    PERMANENT-class rejections)."""
    exc = _sdk_status_error(429, {"error": {"type": "context_length_exceeded"}})
    result = classify_error(exc)
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.context_overflow is True


def test_billing_is_independent_of_error_class() -> None:
    """OpenAI's out-of-credit arrives as a 429 (TRANSIENT — the retry loop
    still retries it, deliberately unchanged), yet it is still a billing
    failure the operator has to clear. The two axes must not be conflated."""
    exc = _sdk_status_error(429, {"error": {"type": "insufficient_quota", "message": "x"}})
    result = classify_error(exc)
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is True


# ───────────── google.genai: `.code` status + `.details` body, read through the wrapper ─────────────

# The exact response body of the 2026-09-07 incident (task #2610): google.genai
# reports exhausted prepaid credits as HTTP 429 / gRPC RESOURCE_EXHAUSTED with
# the reason ONLY in `message` — no `type`, and `code` is the int status.
_GEMINI_PREPAY_DETAILS: Mapping[str, object] = {
    "error": {
        "code": 429,
        "message": (
            "Your prepayment credits are depleted. Please go to AI Studio at "
            "https://ai.studio/projects to manage your project and billing. Learn more "
            "at https://ai.google.dev/gemini-api/docs/billing#prepay. "
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
}


def test_google_genai_prepay_depletion_is_billing() -> None:
    """The raw google.genai ClientError shape: `.code` is the HTTP status (no
    `.status_code`), the body lives in `.details` (no `.body`), and the billing
    reason is free-text prose. The classifier must read all three so the
    first-occurrence billing alert fires — this is the 2026-09-07 incident
    shape that previously logged billing=false / status=None / class=unknown."""
    exc = _genai_error(429, _GEMINI_PREPAY_DETAILS)
    result = classify_error(exc)
    assert result.status == 429
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is True
    # The int status `code` is not a body `error.code` string — it must not be
    # reported as one (and the reported fields stay what the vendor said).
    assert result.error_type is None
    assert result.error_code is None


def test_langchain_wrapper_reveals_chained_genai_billing() -> None:
    """The official retryable Google model wrapper retains typed billing metadata."""
    cause = _genai_error(429, _GEMINI_PREPAY_DETAILS)
    wrapper = _google_wrapper(
        f"Error calling model 'gemini-3.7-flash' (RESOURCE_EXHAUSTED): {cause}", cause
    )
    result = classify_error(wrapper)
    assert result.billing is True
    assert result.status == 429
    assert result.error_class is ErrorClass.TRANSIENT
    # The `provider` label stays the top exception's package (the wrapper) —
    # only the structured fields were read from the cause — so postmortems
    # that already filter gemini events by provider keep matching.
    assert result.provider == type(wrapper).__module__.split(".", 1)[0]


@pytest.mark.parametrize("status", [408, 409, 425])
def test_generic_google_wrapper_cannot_borrow_transient_sdk_cause(status: int) -> None:
    cause = _genai_error(status, {"error": {"message": "request failed"}})
    wrapper = _google_wrapper(str(cause), cause)
    assert not isinstance(wrapper, ModelError)
    assert classify_error(cause).error_class is ErrorClass.TRANSIENT
    classified = classify_error(wrapper)
    assert classified.error_class is ErrorClass.UNKNOWN
    assert classified.status is None


@pytest.mark.parametrize(
    "message",
    [
        # Same API, same status name — rate limiting, not billing. Admitting
        # these would page the operator on ordinary traffic.
        "Quota exceeded for metric aiplatform.googleapis.com/generate_content_requests "
        "per minute per user. Please retry later.",
        "The model is overloaded. Please try again later.",
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric requests per minute.",
    ],
)
def test_google_genai_rate_limit_429_is_not_billing(message: str) -> None:
    """The false-positive guard for the message arm: google.genai rate-limit
    429s share the RESOURCE_EXHAUSTED status name, so only the prepayment
    phrase — never the status — may flip the billing verdict."""
    details = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}
    result = classify_error(_genai_error(429, details))
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is False


def test_prepay_phrase_without_status_is_not_billing() -> None:
    """The message arm is gated on the 429 status: a phrase-only wrapper with
    no chained status (nothing to prove this is Google's billing rejection)
    must stay non-billing rather than page on a hunch."""
    wrapper = _google_wrapper(
        "Error calling model 'gemini-3.7-flash' (RESOURCE_EXHAUSTED): 429 "
        "RESOURCE_EXHAUSTED. Your prepayment credits are depleted.",
        None,
    )
    result = classify_error(wrapper)
    assert result.status is None
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.billing is False


def test_google_genai_400_wrapper_classifies_permanent() -> None:
    """Official ModelInvalidRequestError owns permanent rejection and typed metadata."""
    cause = _genai_error(
        400, {"error": {"message": "invalid argument", "status": "INVALID_ARGUMENT"}}
    )
    wrapper = _google_wrapper(
        f"Error calling model 'gemini-3.7-flash' (INVALID_ARGUMENT): {cause}", cause
    )
    result = classify_error(wrapper)
    assert result.status == 400
    assert result.error_class is ErrorClass.PERMANENT
    assert result.billing is False


def test_google_genai_5xx_classifies_transient() -> None:
    """google.genai server errors (raised raw, not wrapped) carry the 5xx as
    `.code`; they classify TRANSIENT like every other provider's 5xx."""
    exc = _genai_error(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
    result = classify_error(exc)
    assert result.status == 503
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is False


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, None),  # bad key, not an empty one
        (400, {"error": {"type": "invalid_request_error", "message": "prompt too long"}}),
        (429, {"error": {"type": "rate_limit_error", "message": "slow down"}}),
        (500, None),
        # The captured DashScope auth body (2026-08-20) — the same broad `type`
        # an arrears rejection carries, so only `code` separates the two.
        (
            401,
            {
                "error": {
                    "message": "Incorrect API key provided. ",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key",
                }
            },
        ),
        # DashScope's documented TPS/TPM rate limits. They say "quota" and are
        # NOT billing: admitting them would page the operator on normal traffic.
        (429, {"error": {"type": "invalid_request_error", "code": "Throttling.AllocationQuota"}}),
        (429, {"error": {"type": "invalid_request_error", "code": "Throttling.RateQuota"}}),
    ],
)
def test_non_billing_failures_are_not_billing(status: int, body: object) -> None:
    """The false-positive guard: this alert fires on the FIRST occurrence, so a
    plain auth failure or rate limit must never read as "out of credit"."""
    assert classify_error(_sdk_status_error(status, body)).billing is False
