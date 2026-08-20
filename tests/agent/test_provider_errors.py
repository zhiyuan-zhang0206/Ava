"""Unit tests for shared/lm/errors.py:classify_error — the cross-provider
exception taxonomy (transient / permanent / unknown). Sibling to
test_provider_stop.py, which covers the terminal-reason classifier.
"""

import anthropic
import httpx
import openai
import pytest

from shared.lm.errors import ErrorClass, classify_error


class _FakeStatusError(Exception):
    """anthropic/openai APIStatusError shape: an int status_code (+ optional body)."""

    def __init__(self, status_code: object, body: dict | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body  # pyright: ignore[reportUnknownMemberType]


# ───────────── permanent statuses ─────────────


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_permanent_statuses(status: int) -> None:
    """400 (bad request / context length / schema), 401 auth, 402 billing, 403
    forbidden, 404 unknown model, 422 schema — deterministic within the turn."""
    result = classify_error(_FakeStatusError(status))
    assert result.error_class is ErrorClass.PERMANENT
    assert result.status == status


def test_context_length_400_is_permanent_not_retried() -> None:
    """The headline gap this taxonomy closes: a context-overflow 400 is PERMANENT,
    so the node fails fast + idles instead of burning the full retry budget and
    dying. The classifier does not need the message text — the status is enough."""
    exc = _FakeStatusError(
        400, {"error": {"type": "invalid_request_error", "message": "prompt is too long"}}
    )
    result = classify_error(exc)
    assert result.error_class is ErrorClass.PERMANENT
    assert result.error_type == "invalid_request_error"


# ───────────── transient statuses ─────────────


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504, 529])
def test_transient_statuses(status: int) -> None:
    """429 rate limit, 5xx server (>= 500 range), 408/409/425 — retryable in-turn."""
    result = classify_error(_FakeStatusError(status))
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.status == status


# ───────────── unknown statuses ─────────────


@pytest.mark.parametrize("status", [411, 413, 415, 418])
def test_unrecognized_status_is_unknown_not_guessed(status: int) -> None:
    """A 4xx we don't explicitly place (413 payload-too-large etc.) is UNKNOWN,
    never guessed into permanent or transient — it propagates through the normal
    retry path and surfaces if it persists."""
    result = classify_error(_FakeStatusError(status))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.status == status


# ───────────── transport (no status) → transient ─────────────


def test_transport_errors_are_transient() -> None:
    """Connection / timeout failures carry no HTTP status; the SDKs' own base
    classes (+ builtins) classify them TRANSIENT so the retry path covers them."""
    req = httpx.Request("POST", "http://x")
    transport_excs: list[BaseException] = [
        anthropic.APIConnectionError(request=req),
        anthropic.APITimeoutError(request=req),  # subclass of APIConnectionError
        openai.APIConnectionError(request=req),
        openai.APITimeoutError(request=req),
        httpx.ReadError("read reset"),
        httpx.ConnectError("refused"),
        httpx.PoolTimeout("pool"),
        ConnectionError("net"),
        TimeoutError("slow"),
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
    result = classify_error(_FakeStatusError("429"))
    assert result.status is None
    assert result.error_class is ErrorClass.UNKNOWN


def test_error_type_extracted_from_body() -> None:
    result = classify_error(
        _FakeStatusError(429, {"error": {"type": "engine_overloaded_error", "message": "busy"}})
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
    result = classify_error(_FakeStatusError(500, body))  # type: ignore[arg-type]
    assert result.error_type is None
    assert result.error_class is ErrorClass.TRANSIENT


# ───────────── provider label ─────────────


def test_provider_label_from_module() -> None:
    """provider is the top-level package that raised — the coarse postmortem label."""
    req = httpx.Request("POST", "http://x")
    assert classify_error(anthropic.APIConnectionError(request=req)).provider == "anthropic"
    assert classify_error(openai.APIConnectionError(request=req)).provider == "openai"
    assert classify_error(httpx.ReadError("x")).provider == "httpx"
    assert classify_error(ValueError("x")).provider == "builtins"


# ───────────── billing / quota exhaustion ─────────────


def test_402_is_billing() -> None:
    """HTTP 402 Payment Required is the unambiguous cross-provider signal
    (DeepSeek returns it for `Insufficient Balance`)."""
    result = classify_error(_FakeStatusError(402))
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
    exc = _FakeStatusError(429, {"error": {"type": error_type, "message": "no balance"}})
    assert classify_error(exc).billing is True


def test_billing_is_independent_of_error_class() -> None:
    """OpenAI's out-of-credit arrives as a 429 (TRANSIENT — the RetryPolicy
    still retries it, deliberately unchanged), yet it is still a billing
    failure the operator has to clear. The two axes must not be conflated."""
    exc = _FakeStatusError(429, {"error": {"type": "insufficient_quota", "message": "x"}})
    result = classify_error(exc)
    assert result.error_class is ErrorClass.TRANSIENT
    assert result.billing is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, None),  # bad key, not an empty one
        (400, {"error": {"type": "invalid_request_error", "message": "prompt too long"}}),
        (429, {"error": {"type": "rate_limit_error", "message": "slow down"}}),
        (500, None),
    ],
)
def test_non_billing_failures_are_not_billing(status: int, body: object) -> None:
    """The false-positive guard: this alert fires on the FIRST occurrence, so a
    plain auth failure or rate limit must never read as "out of credit"."""
    assert classify_error(_FakeStatusError(status, body)).billing is False  # type: ignore[arg-type]
