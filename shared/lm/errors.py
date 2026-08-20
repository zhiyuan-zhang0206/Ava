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

Crossing all three, `ErrorClassification.billing` answers a different question —
"is this the key running out of money?" — because that is the one failure no
retry policy can clear, and it is what the billing/quota alert fires on.
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
# `tests/shared/test_qwen_live_smoke.py`)::
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
# What is still unverified: every entry below except the DeepSeek 402 path comes
# from vendor documentation, not from a captured live 4xx. For DashScope matching
# both fields removes one unknown and leaves one. Removed: WHICH field carries an
# arrears reason no longer decides whether the alert can fire. Remaining: the
# SPELLING. Alibaba documents its arrears codes in a single table that never says
# which entries the OpenAI-compatible endpoint re-spells, and its compatible-mode
# page publishes exactly one code — `invalid_api_key`, the body captured above.
# So the documented PascalCase is what goes in; the case-fold absorbs a casing
# difference, but a different WORD on the wire would still miss.
#
# A wrong or missing string costs a MISSED alert, never a false one — but silence
# here is indistinguishable from health: an unmatched string means nothing is
# ever tagged and the rule stays quiet forever, so no incident "surfaces" the gap
# on its own. Two things close it, both operator-driven: the first real arrears
# rejection (capture the raw body and reconcile it with this set), or an opt-in
# live run against a drained key — `tests/shared/test_qwen_live_smoke.py` carries
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
    error_code: str | None  # provider body `error.code` — read by `billing` only, never logged

    @property
    def billing(self) -> bool:
        """True when the provider refused because the key is out of credit or
        its quota is exhausted (`_BILLING_STATUS` / `_BILLING_ERROR_VOCABULARY`).

        A derived view, not a field: it is a reading of the same
        (status, error_type, error_code) the classifier already carries, so it
        cannot drift out of sync with them. Both body fields are matched — not
        `code` as a fallback for an absent `type`, which would never reach the
        vendor that needs it (see the vocabulary comment). Deliberately
        independent of `error_class` — an out-of-credit key arrives as a
        PERMANENT 402 from one vendor and a TRANSIENT 429 from another, and the
        operator has to top the key up either way.
        """
        if self.status == _BILLING_STATUS:
            return True
        return any(
            field is not None and field.lower() in _BILLING_ERROR_VOCABULARY
            for field in (self.error_type, self.error_code)
        )


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


def _error_field_of(exc: BaseException, key: str) -> str | None:
    """One string field of the provider response body's `error` object, or None.

    Both SDKs shape errors as ``{"error": {"type": ..., "code": ..., "message":
    ...}}`` on `.body` — `type` the broad class, `code` (when the vendor sends
    one) the specific reason. Returns None when `body` is absent / not a dict,
    `error` is missing / not a dict, or the field is missing / not a non-empty
    string.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = cast("dict[str, Any]", body).get("error")
    if not isinstance(error, dict):
        return None
    value = cast("dict[str, Any]", error).get(key)
    return value if isinstance(value, str) and value else None


def classify_error(exc: BaseException) -> ErrorClassification:
    """Map a provider-SDK / transport exception to its `ErrorClassification`.

    Status-first (both SDKs carry `status_code` on API errors), then transport
    (no status), then UNKNOWN. Never raises — an unrecognized exception is
    UNKNOWN, not a guess.
    """
    provider = _provider_of(exc)
    error_type = _error_field_of(exc, "type")
    error_code = _error_field_of(exc, "code")
    status = _status_of(exc)
    if status is not None:
        if status in _PERMANENT_STATUSES:
            error_class = ErrorClass.PERMANENT
        elif status in _TRANSIENT_STATUSES or status >= 500:
            error_class = ErrorClass.TRANSIENT
        else:
            error_class = ErrorClass.UNKNOWN
        return ErrorClassification(error_class, provider, status, error_type, error_code)
    if _is_transport_error(exc):
        return ErrorClassification(ErrorClass.TRANSIENT, provider, None, error_type, error_code)
    return ErrorClassification(ErrorClass.UNKNOWN, provider, None, error_type, error_code)
