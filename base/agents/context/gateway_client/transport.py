"""HTTP transport and wire-error translation for the gateway SDK client."""

from __future__ import annotations

import json as _json
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from base.agents.context import MeivaContext
from base.agents.context.clients import GatewayHandle
from base.agents.messages.delivery.retry import GatewayResponse, network_errors, retryable_response
from base.api_contracts import contracts
from base.api_contracts.agent_contract import EXCEPTION_BY_REASON, ErrorReason, GatewayUnavailable
from base.api_contracts.contracts import Idempotency
from base.api_contracts.idempotency import keyed_headers
from base.host.net.resilience import Policy, http_classifier, retry

__all__ = [
    "GatewayTransportInputs",
    "TransportResponse",
    "TransportTimeout",
    "get",
    "patch",
    "post",
    "raise_from_response",
]

# httpx stays off the `import meiva` path: every function that needs it imports it in its body, and
# the annotations below are structural (`TransportResponse`, `TransportTimeout`) or the client
# alias owned by `base.agents.context.clients`.


class TransportResponse(GatewayResponse, Protocol):
    """The slice of ``httpx.Response`` the SDK reads from a gateway reply."""

    @property
    def is_success(self) -> bool: ...

    def raise_for_status(self) -> object: ...


class TransportTimeout(Protocol):
    """The ``httpx.Timeout`` a caller passes to override one request's client timeout."""

    @property
    def connect(self) -> float | None: ...

    @property
    def read(self) -> float | None: ...

    @property
    def write(self) -> float | None: ...

    @property
    def pool(self) -> float | None: ...


@dataclass(frozen=True, kw_only=True)
class GatewayTransportInputs:
    """One context's live gateway retry and memory-search deadline readers.

    Construction performs no reads and owns no HTTP connection. The process
    root configures this component through its existing ClientSet factory map.
    """

    max_retries_reader: Callable[[], int]
    retry_delay_reader: Callable[[], float]
    memory_deadline_reader: Callable[[], float]


def _inputs(context: MeivaContext) -> GatewayTransportInputs:
    return context.clients.get(GatewayTransportInputs)


def gateway_http(context: MeivaContext) -> GatewayHandle:
    """The bound context's gateway client, built on first use so importing the SDK in a no-config
    context does not require the gateway URL to be resolvable until an actual call is made. Its
    timeout is `Settings.gateway_client_http_timeout_seconds` (env override
    `MEIVA_GATEWAY_HTTP_TIMEOUT_SECONDS`): most gateway ops are near-instant, but spawn does real
    server-side work (launch the new process + poll until it claims its row), so the budget must
    comfortably exceed the server's confirm window, otherwise a read timeout on a spawn that DID
    succeed triggers a retry that re-POSTs the non-idempotent create and yields a phantom-twin
    agent. `use_client` routes the calls through a different one."""
    return context.gateway


# Gateway cold start ~0.6s (measured). Default 3 retries, base interval 1s —
# bounded exponential backoff 1s → 2s → 4s → 8s cap (see `retry_delay_seconds`),
# a much larger window than the restart time and well within the 10s timeout.
# env override: `MEIVA_GATEWAY_MAX_RETRIES` / `MEIVA_GATEWAY_RETRY_DELAY_SECONDS`.


def request_max_retries(context: MeivaContext) -> int:
    return _inputs(context).max_retries_reader()


def base_retry_delay_s(context: MeivaContext) -> float:
    return _inputs(context).retry_delay_reader()


# ── Transient-failure retry policy ──
# The gateway response policy lives in base.agents.messages.delivery.retry.
# SDK and CLI bounded retry share its known statuses
# (429/502/503/504) and respect committed/retryable wire controls.

# Bounded exponential backoff: attempt i sleeps the base delay multiplied by
# the backoff factor raised to i, capped at `_RETRY_MAX_DELAY_S`, plus a
# deterministic per-agent jitter offset (`agent_jitter_seconds`) that
# de-phases retry waves across the fleet (heartbeat-daemon pattern, see below).
_RETRY_BACKOFF_FACTOR = 2.0
_RETRY_MAX_DELAY_S = 8.0
# Span of the per-agent jitter offset, seconds.
JITTER_SPAN_S = 5.0


def agent_jitter_seconds(context: MeivaContext) -> float:
    """Deterministic per-agent offset in [0, JITTER_SPAN_S); 0 when no agent id.

    The heartbeat daemon's per-agent due-time jitter pattern
    (services/wake/heartbeat/daemon.py): a correlated gateway outage hits every
    agent at the same moment, and a fleet-wide identical retry schedule
    (1s, 2s, 4s, ...) would re-synchronize the retry waves as each agent
    retries in lockstep. Offsetting every sleep by a stable per-agent amount
    keeps the waves de-phased. Deterministic on the supplied context identity, so an agent
    keeps its own offset across restarts; no identity
    (tests, non-agent callers) → 0 (no offset).
    """
    identity = context.identity
    ident = identity.lease.validate() if identity is not None and identity.lease else None
    if ident is None and identity is not None:
        ident = identity.agent_id
    if ident is None:
        return 0.0
    return JITTER_SPAN_S * (ident % 1000) / 1000.0


def retry_delay_seconds(attempt: int, context: MeivaContext) -> float:
    """Sleep before retry `attempt` (0-based): bounded exponential backoff
    plus the deterministic per-agent jitter offset."""
    base = min(base_retry_delay_s(context) * _RETRY_BACKOFF_FACTOR**attempt, _RETRY_MAX_DELAY_S)
    return base + agent_jitter_seconds(context)


def raise_from_response(resp: TransportResponse) -> None:
    """Non-2xx → rebuild exception per wire contract. Flow:

    1. body JSON parse failure (FastAPI default 500 plain text, corrupted
       content-encoding → httpx.DecodingError, etc.) →
       call `resp.raise_for_status()` raising `httpx.HTTPStatusError`,
       full status + body preview shown to caller
    2. JSON OK but `reason` field missing / `ErrorReason(...)` unrecognized /
       `detail` missing or not a string → same as above; signal of wire
       protocol broken should not be silently swallowed
    3. JSON has valid reason → reverse-lookup EXCEPTION_BY_REASON to rebuild the corresponding exception

    Steps 1-2 run `raise_for_status` OUTSIDE any except handler: an
    HTTPStatusError raised while a KeyError/JSONDecodeError is being handled
    is chained to it, so the traceback leads with `KeyError: 'reason'` and
    the original status code is buried (2026-08-12 send_message 503 report,
    task #1205). The wire parse (`_wire_reason`) never leaks its own
    exception — a protocol mismatch surfaces as the clean HTTP error.
    """
    if resp.is_success:
        return
    wire = _wire_reason(resp)
    if wire is None:
        # Body doesn't match the wire contract (non-JSON, missing or
        # unrecognized `reason`). Propagate the raw HTTP error to the
        # caller, fail fast. raise_for_status on non-2xx always raises
        # (precondition for this branch guaranteed); it runs outside any
        # except handler so the HTTPStatusError is not chained to a parse
        # exception that masks the status code.
        resp.raise_for_status()
        return  # unreachable; raise_for_status has raised
    reason, body = wire
    if reason == ErrorReason.AGENT_LAUNCH_FAILED:
        from base.api_contracts.agent_contract import AgentLaunchFailed

        raise AgentLaunchFailed(
            body["detail"],
            agent_id=body.get("agent_id"),
            state=body.get("state"),
            retry_launch_path=body.get("retry_launch_path"),
        )
    raise EXCEPTION_BY_REASON[reason](body["detail"])


def _wire_reason(resp: TransportResponse) -> tuple[ErrorReason, dict[str, Any]] | None:
    """Parse the wire `reason` from a non-2xx response body, or None.

    None means the body does not carry a valid wire-contract reason (not
    JSON, not an object, `reason` field missing, value not in
    `ErrorReason`, or `detail` missing / not a string) — the caller then
    surfaces the raw HTTP error. Never raises: a protocol mismatch must
    fall through to `raise_for_status` (HTTPStatusError with the original
    status code), not escape as a KeyError/JSONDecodeError that masks it.
    """
    import httpx

    try:
        raw_body = resp.json()
    except (_json.JSONDecodeError, httpx.DecodingError):
        # JSONDecodeError: body is not JSON (FastAPI default 500 text, ...).
        # DecodingError: httpx 0.28.1 raises it when body decoding fails on a
        # corrupted Content-Encoding (broken gzip/br stream) — same
        # protocol-mismatch class; falls through to the clean HTTP error
        # instead of escaping and masking the status code.
        return None
    if not isinstance(raw_body, dict):
        return None
    body = cast(dict[str, Any], raw_body)
    try:
        reason = ErrorReason(body["reason"])
    except (KeyError, ValueError):
        return None
    if not isinstance(body.get("detail"), str):
        # Valid reason but no string `detail` — the same protocol mismatch
        # class: the reverse-lookup raise would KeyError on `body["detail"]`
        # and mask the status code exactly like the old missing-`reason`
        # path did (task #1205). Fall through to the clean HTTP error.
        return None
    return reason, body


class _TransientResponseError(Exception):
    """Carry the original HTTP response through the exception-based retry executor."""

    def __init__(self, response: TransportResponse) -> None:
        self.response = response


def _request_with_retry(
    request: Callable[[], TransportResponse],
    attempts: int,
    *,
    retryable: bool = True,
    context: MeivaContext,
) -> TransportResponse:
    """Execute one route's policy while keeping its final wire response intact."""
    import httpx

    transport_errors = network_errors()
    pre_send_errors = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    gateway_classifier = http_classifier.with_(permanent={500})

    def classify(exc: Exception) -> bool:
        if not retryable:
            # A non-idempotent POST can only be repeated when nothing was sent.
            return isinstance(exc, pre_send_errors)
        if isinstance(exc, httpx.TransportError) and not isinstance(exc, transport_errors):
            return False
        return gateway_classifier(exc)

    def once() -> TransportResponse:
        resp = request()
        if retryable and retryable_response(resp):
            raise _TransientResponseError(resp)
        return resp

    if attempts < 1:
        # Preserve the old range(attempts) behavior for a zero/negative override.
        raise GatewayUnavailable(
            f"Gateway transport error at {gateway_http(context).base_url} (after {attempts} retries): None"
        )
    policy = Policy(
        max_attempts=attempts,
        backoff=lambda attempt: retry_delay_seconds(attempt, context),
        jitter="none",  # backoff already includes the exact agent-only phase
        classify=classify,
        respect_retry_after=False,
    )
    try:
        return retry(policy)(once)
    except _TransientResponseError as exc:
        # The caller still owns raise_from_response and its wire-reason mapping.
        return exc.response
    except transport_errors as exc:
        if not retryable and not isinstance(exc, pre_send_errors):
            raise GatewayUnavailable(
                f"Gateway transport error at {gateway_http(context).base_url} "
                f"(no retry: non-idempotent request; result unknown, may have been delivered): {exc!s}"
            ) from exc
        raise GatewayUnavailable(
            f"Gateway transport error at {gateway_http(context).base_url} (after {attempts} retries): {exc!s}"
        ) from exc


def _post_semantics(path: str, *, idempotent: bool | None) -> Idempotency:
    """The retry semantics of one POST: the caller's explicit override, else the route's contract."""
    if idempotent is None:
        return contracts.idempotency_for("POST", path)
    return Idempotency.IDEMPOTENT if idempotent else Idempotency.NON_IDEMPOTENT


def post(
    path: str,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: TransportTimeout | None = None,
    *,
    idempotent: bool | None = None,
    idempotency_key: str | None = None,
    max_retries: int | None = None,
    context: MeivaContext,
) -> TransportResponse:
    """Unified POST wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Retries known network failures and HTTP 429/502/503/504 responses only when the route
    is idempotent or promises server-side deduplication. For non-idempotent
    routes, only ConnectError, ConnectTimeout, and PoolTimeout are retried:
    these happen before the request can be sent. Other transport failures
    leave the result unknown and must not cause a second POST. Retries use
    bounded exponential backoff + per-agent jitter
    (`retry_delay_seconds`), then either raises `GatewayUnavailable`
    (transport) or returns the last response (HTTP: the caller's
    `raise_from_response` produces the wire error / `HTTPStatusError` — the
    loud failure carries the full status + body).

    `idempotent=None` (default) inherits the route's semantics from its
    doorplate (`base.api_contracts.contracts`): IDEMPOTENT retries the transient family,
    NON_IDEMPOTENT surfaces an uncertain transport failure immediately (the
    server may have acted on it — re-sending can duplicate the effect, e.g.
    a pure INSERT), and AT_LEAST_ONCE_WITH_KEY retries with an
    `Idempotency-Key` + `Idempotency-Scope: principal-v1` header pair (one key
    per logical call, shared by all retries) — the server dedups, so
    re-sending is safe. An explicit `idempotent=...` overrides the contract
    (kept for callers that know better); `idempotency_key` pins the key for
    a caller that wants to control it. The connect family is always
    retried: those fail before anything reached the server.

    `timeout` overrides the per-client default for this single request;
    callers that expect a larger body or server-side work (e.g. send_message
    with a long message body) pass a per-call timeout. `None` means "use the
    client's configured timeout" — there is deliberately no way to ask for an
    unbounded request.

    `max_retries` overrides the module-wide attempt count (`request_max_retries()`) for
    this one call. The default fits routes the gateway works on directly, but
    a call whose failure mode is a *modelled, already-spent* response (e.g.
    memory search, where the gateway answers 503 only after consuming its own
    budget) should shrink it — re-sending such a request just waits behind the
    same congestion.
    """
    import httpx

    # httpx reads an explicit `timeout=None` as "wait forever"; its own way of
    # saying "use the client's configured timeout" is the USE_CLIENT_DEFAULT
    # sentinel, which is what the argument defaults to when left off. Forwarding
    # our None straight through therefore disabled the client's timeout on every
    # POST in this module rather than falling back to it, so a gateway route that
    # stopped responding parked its SDK callers forever instead of raising.
    per_call = httpx.USE_CLIENT_DEFAULT if timeout is None else cast(httpx.Timeout, timeout)

    # Inherit retry semantics from the route's doorplate unless the caller
    # overrides: server promises (base/api_contracts/contracts.py), clients inherit.
    semantics = _post_semantics(path, idempotent=idempotent)
    keyed = semantics is Idempotency.AT_LEAST_ONCE_WITH_KEY
    retryable = semantics is Idempotency.IDEMPOTENT or keyed
    # One key per logical call — every retry of this call shares it, so the
    # server can dedup the retries against the original.
    headers = (
        keyed_headers(_uuid.uuid4().hex if idempotency_key is None else idempotency_key)
        if keyed
        else None
    )

    retries = request_max_retries(context) if max_retries is None else max_retries
    return _request_with_retry(
        lambda: gateway_http(context).post(
            path, json=json or {}, params=params, timeout=per_call, headers=headers
        ),
        retries,
        retryable=retryable,
        context=context,
    )


def get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: TransportTimeout | None = None,
    max_retries: int | None = None,
    context: MeivaContext,
) -> TransportResponse:
    """Unified GET wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Same policy as `post`; a GET is always idempotent, so transient HTTP
    429/5xx responses are retried too.

    `timeout` overrides the per-client default for this single request;
    `max_retries` overrides the module-wide attempt count. Both exist for the
    same reason `post` has them: a caller on a hot, must-not-stall path (the
    born-chain read at context establishment) shrinks its budget for a request
    whose failure it already knows how to degrade — re-sending it only parks
    the agent's birth behind backoff it cannot use.
    """
    import httpx

    per_call = httpx.USE_CLIENT_DEFAULT if timeout is None else cast(httpx.Timeout, timeout)
    retries = request_max_retries(context) if max_retries is None else max_retries
    return _request_with_retry(
        lambda: gateway_http(context).get(path, params=params, timeout=per_call),
        retries,
        context=context,
    )


def patch(
    path: str,
    json: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    context: MeivaContext,
) -> TransportResponse:
    """Unified PATCH wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Retries ambiguous failures when the route declares natural idempotency or
    promises server-side deduplication: a keyed route sends the same key and
    `principal-v1` scope on every retry. A PATCH verb alone does not prove its
    business effects are repeatable. Pass the original key to replay.
    """
    semantics = contracts.idempotency_for("PATCH", path)
    keyed = semantics is Idempotency.AT_LEAST_ONCE_WITH_KEY
    headers = (
        keyed_headers(_uuid.uuid4().hex if idempotency_key is None else idempotency_key)
        if keyed
        else None
    )
    return _request_with_retry(
        lambda: gateway_http(context).patch(path, json=json or {}, headers=headers),
        request_max_retries(context),
        retryable=keyed or semantics is Idempotency.IDEMPOTENT,
        context=context,
    )
