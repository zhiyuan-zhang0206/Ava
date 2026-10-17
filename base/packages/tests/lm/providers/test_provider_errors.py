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


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_permanent_statuses(status: int) -> None:
    result = classify_error(_sdk_status_error(status))
    assert result.error_class is ErrorClass.PERMANENT
    assert result.status == status


def test_context_length_400_is_permanent_not_retried() -> None:
    exc = _sdk_status_error(
        400, {"error": {"type": "invalid_request_error", "message": "prompt is too long"}}
    )
    result = classify_error(exc)
    assert result.error_class is ErrorClass.PERMANENT
    assert result.error_type == "invalid_request_error"


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504, 529])
def test_transient_statuses(status: int) -> None:
    result = classify_error(_sdk_status_error(status))
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.status == status


@pytest.mark.parametrize("status", [411, 413, 415, 418])
def test_unrecognized_status_is_unknown_not_guessed(status: int) -> None:
    result = classify_error(_sdk_status_error(status))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.status == status


def test_transport_errors_are_transient() -> None:
    req = httpx2.Request("POST", "http://x")
    transport_excs: list[BaseException] = [
        anthropic.APIConnectionError(request=req),
        anthropic.APITimeoutError(request=req),
        openai.APIConnectionError(request=req),
        openai.APITimeoutError(request=req),
    ]
    for exc in transport_excs:
        result = classify_error(exc)
        assert result.error_class is ErrorClass.TRANSIENT, exc
        assert result.status is None


def test_unrecognized_exception_is_unknown() -> None:
    result = classify_error(ValueError("something we've never seen"))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.status is None


def test_non_int_status_code_falls_through() -> None:

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
    assert result.error_class is ErrorClass.TRANSIENT


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
    result = classify_error(_sdk_status_error(500, body))
    assert result.error_type is None
    assert result.error_class is ErrorClass.TRANSIENT


def test_provider_label_from_module() -> None:
    req = httpx2.Request("POST", "http://x")
    assert classify_error(anthropic.APIConnectionError(request=req)).provider == "anthropic"
    assert classify_error(openai.APIConnectionError(request=req)).provider == "openai"
    assert classify_error(httpx.ReadError("x")).provider == "httpx"
    assert classify_error(ValueError("x")).provider == "builtins"


def test_402_is_billing() -> None:
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
        "Arrearage",
    ],
)
def test_billing_error_types_are_billing(error_type: str) -> None:
    exc = _sdk_status_error(429, {"error": {"type": error_type, "message": "no balance"}})
    assert classify_error(exc).billing is True


@pytest.mark.parametrize(
    "error_code",
    [
        "Arrearage",
        "PrepaidBillOverdue",
        "PostpaidBillOverdue",
        "arrearage",
    ],
)
def test_dashscope_billing_codes_are_billing(error_code: str) -> None:
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    exc = _sdk_status_error(
        400,
        {"error": {"type": "invalid_request_error", "code": error_code, "message": "no balance"}},
    )
    assert classify_error(exc).billing is True


def test_billing_code_match_does_not_widen_the_reported_error_type() -> None:
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    exc = _sdk_status_error(
        400, {"error": {"type": "invalid_request_error", "code": "Arrearage", "message": "x"}}
    )
    result = classify_error(exc)
    assert result.billing is True
    assert result.error_type == "invalid_request_error"


def test_context_overflow_400_with_length_message() -> None:
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
    exc = _sdk_status_error(400, {"error": {"type": error_type}})
    assert classify_error(exc).context_overflow is True


def test_schema_400_is_not_context_overflow() -> None:
    exc = _sdk_status_error(
        400, {"error": {"type": "invalid_request_error", "message": "bad schema"}}
    )
    result = classify_error(exc)
    assert result.error_class is ErrorClass.PERMANENT
    assert result.context_overflow is False


def test_rate_limit_429_mentioning_tokens_is_not_overflow() -> None:
    exc = _sdk_status_error(
        429, {"error": {"type": "rate_limit_error", "message": "too many tokens per minute"}}
    )
    assert classify_error(exc).context_overflow is False


def test_429_with_context_length_exceeded_type_stays_transient() -> None:
    exc = _sdk_status_error(429, {"error": {"type": "context_length_exceeded"}})
    result = classify_error(exc)
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.context_overflow is True


def test_billing_is_independent_of_error_class() -> None:
    exc = _sdk_status_error(429, {"error": {"type": "insufficient_quota", "message": "x"}})
    result = classify_error(exc)
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is True


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
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    exc = _genai_error(429, _GEMINI_PREPAY_DETAILS)
    result = classify_error(exc)
    assert result.status == 429
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is True
    assert result.error_type is None
    assert result.error_code is None


def test_langchain_wrapper_reveals_chained_genai_billing() -> None:
    cause = _genai_error(429, _GEMINI_PREPAY_DETAILS)
    wrapper = _google_wrapper(
        f"Error calling model 'gemini-3.7-flash' (RESOURCE_EXHAUSTED): {cause}", cause
    )
    result = classify_error(wrapper)
    assert result.billing is True
    assert result.status == 429
    assert result.error_class is ErrorClass.TRANSIENT
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
        "Quota exceeded for metric aiplatform.googleapis.com/generate_content_requests "
        "per minute per user. Please retry later.",
        "The model is overloaded. Please try again later.",
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric requests per minute.",
    ],
)
def test_google_genai_rate_limit_429_is_not_billing(message: str) -> None:
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    details = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}
    result = classify_error(_genai_error(429, details))
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is False


def test_prepay_phrase_without_status_is_not_billing() -> None:
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
    exc = _genai_error(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})
    result = classify_error(exc)
    assert result.status == 503
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is False


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, None),
        (400, {"error": {"type": "invalid_request_error", "message": "prompt too long"}}),
        (429, {"error": {"type": "rate_limit_error", "message": "slow down"}}),
        (500, None),
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
        (429, {"error": {"type": "invalid_request_error", "code": "Throttling.AllocationQuota"}}),
        (429, {"error": {"type": "invalid_request_error", "code": "Throttling.RateQuota"}}),
    ],
)
def test_non_billing_failures_are_not_billing(status: int, body: object) -> None:
    assert classify_error(_sdk_status_error(status, body)).billing is False
