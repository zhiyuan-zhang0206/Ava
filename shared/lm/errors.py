"""Cross-provider classification of LLM provider-SDK exceptions.

Sibling to `shared/lm/stop.py`: where that normalizes a *successful* response's
terminal reason, this normalizes a *failed* call's exception into one
provider-agnostic `ErrorClass`, so the LLM node decides retry-in-turn vs
fail-fast-to-idle without scattering `isinstance` / `status_code` checks, and the
postmortem log carries a structured `(error_class, provider, status)` triple
instead of a scraped message string.

LangChain surfaces each provider SDK's own exception unchanged: `anthropic.*` for
DeepSeek + Claude (both on the anthropic client), `openai.*` for GPT, with
`httpx.*` transport errors underneath any of them. anthropic and openai share one
shape — an `APIStatusError` carrying an int `status_code`, and
`APIConnectionError` / `APITimeoutError` (no status) for transport failure — so a
`status_code` + `isinstance` check covers both without a per-provider branch.

Three classes, fail-fast:

- `TRANSIENT`  — retry in-turn: 429 rate limit, 5xx server, 408/409/425, and
  transport (connection / timeout) errors. The tuned LangGraph `RetryPolicy`
  (`agent/graph/_build.py`) already retries these; classification only labels
  them so the postmortem can tell an expected retry from a surprise.
- `PERMANENT`  — retrying the identical request cannot flip it: 400 (bad request
  / context length / malformed / schema), 401 auth, 402 billing, 403 forbidden,
  404 unknown model, 422 schema. The LLM node raises `FatalProviderError` (which
  the `RetryPolicy` excludes) so the agent idles — stays alive — instead of
  burning the full backoff budget and dying.
- `UNKNOWN`    — an exception (or status) we do not recognize. Never guessed into
  either bucket: it propagates through the node's normal path (retried like a
  transient by the `RetryPolicy`, then surfaced if it persists) and is logged as
  `unknown` so a postmortem can spot a gap to close here.
"""

from __future__ import annotations

import enum
from typing import Any, NamedTuple, cast

import httpx


class ErrorClass(enum.Enum):
    TRANSIENT = "transient"  # retryable in-turn -> falls through to the RetryPolicy
    PERMANENT = "permanent"  # deterministic within the turn -> FatalProviderError, idle
    UNKNOWN = "unknown"  # unrecognized -> propagate (retried, then surfaced); do not guess


# HTTP statuses where retrying the identical request is futile within the turn.
# 400 (bad request / context length / malformed / schema) + 422 (unprocessable)
# are the headline additions over the old 401/402/403-only permanent set: a
# context-overflow 400 used to burn the whole 6-retry (~16-min) budget and then
# kill the agent, when it should fail fast and idle.
_PERMANENT_STATUSES: frozenset[int] = frozenset({400, 401, 402, 403, 404, 422})

# 4xx that are genuinely retryable (429 rate limit, 408 request timeout, 409
# conflict, 425 too-early). 5xx is caught by the `>= 500` range, not this set.
_TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 409, 425, 429})


def _is_transport_error(exc: BaseException) -> bool:
    """True for transport-layer failures (no HTTP status).

    Duck-typed by module + class name for the provider SDKs: classifying an
    error must not import openai/anthropic (~11-15MB each) just to run an
    isinstance. In both SDKs `APIConnectionError` covers the `APITimeoutError`
    subclass (same name check). httpx and the builtins are isinstance-checked —
    httpx is already part of the process base.
    """
    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    if type(exc).__module__.split(".", 1)[0] in ("openai", "anthropic"):
        # MRO class names, not just type(exc).__name__: both SDKs' APITimeoutError
        # subclasses APIConnectionError, and isinstance semantics must be kept.
        return "APIConnectionError" in {c.__name__ for c in type(exc).__mro__}
    return False


class ErrorClassification(NamedTuple):
    error_class: ErrorClass
    provider: str  # top-level package that raised: anthropic / openai / httpx / builtins / ...
    status: int | None  # HTTP status_code when the SDK carries one, else None
    error_type: str | None  # provider body `error.type` (e.g. engine_overloaded_error), else None


def _provider_of(exc: BaseException) -> str:
    """Top-level package of the raising exception — the coarse provider label
    (`anthropic` / `openai` / `httpx` / `builtins` / ...) for the postmortem."""
    return type(exc).__module__.split(".", 1)[0]


def _status_of(exc: BaseException) -> int | None:
    """The provider SDK's int HTTP `status_code`, or None.

    Duck-typed (not an `isinstance` on `APIStatusError`) so a wrapper that
    re-exposes `status_code` still classifies, and one that hides it falls
    through to transport / UNKNOWN — no worse than before.
    """
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _error_type_of(exc: BaseException) -> str | None:
    """Provider response body `error.type`, or None.

    Both SDKs shape errors as ``{"error": {"type": ..., "message": ...}}`` on
    `.body`. Returns None when `body` is absent / not a dict, `error` is missing /
    not a dict, or `type` is missing / not a non-empty string.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = cast("dict[str, Any]", body).get("error")
    if not isinstance(error, dict):
        return None
    error_type = cast("dict[str, Any]", error).get("type")
    return error_type if isinstance(error_type, str) and error_type else None


def classify_error(exc: BaseException) -> ErrorClassification:
    """Map a provider-SDK / transport exception to its `ErrorClassification`.

    Status-first (both SDKs carry `status_code` on API errors), then transport
    (no status), then UNKNOWN. Never raises — an unrecognized exception is
    UNKNOWN, not a guess.
    """
    provider = _provider_of(exc)
    error_type = _error_type_of(exc)
    status = _status_of(exc)
    if status is not None:
        if status in _PERMANENT_STATUSES:
            error_class = ErrorClass.PERMANENT
        elif status in _TRANSIENT_STATUSES or status >= 500:
            error_class = ErrorClass.TRANSIENT
        else:
            error_class = ErrorClass.UNKNOWN
        return ErrorClassification(error_class, provider, status, error_type)
    if _is_transport_error(exc):
        return ErrorClassification(ErrorClass.TRANSIENT, provider, None, error_type)
    return ErrorClassification(ErrorClass.UNKNOWN, provider, None, error_type)
