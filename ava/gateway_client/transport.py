"""HTTP transport and wire-error translation for the gateway SDK client."""

from __future__ import annotations

import json as _json
import uuid as _uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import httpx

import ava
from base.agents import EXCEPTION_BY_REASON, ErrorReason, GatewayUnavailable
from base.agents.context import AvaContext
from base.agents.messages.delivery.retry import NETWORK_ERRORS, retryable_response
from base.api_contracts import contracts
from base.api_contracts.contracts import Idempotency
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER, validate_idempotency_key
from base.config import settings
from base.host.net.resilience import Policy, http_classifier, retry


def _http(context: AvaContext | None = None) -> httpx.Client:  # pyright: ignore[reportUndefinedVariable]
    """The bound context's gateway client, built on first use so importing the SDK in a no-config
    context does not require the gateway URL to be resolvable until an actual call is made. Its
    timeout is `Settings.gateway_client_http_timeout_seconds` (env override
    `AVA_GATEWAY_HTTP_TIMEOUT_SECONDS`): most gateway ops are near-instant, but spawn does real
    server-side work (launch the new process + poll until it claims its row), so the budget must
    comfortably exceed the server's confirm window, otherwise a read timeout on a spawn that DID
    succeed triggers a retry that re-POSTs the non-idempotent create and yields a phantom-twin
    agent. `use_client` routes the calls through a different one."""
    return (context if context is not None else ava.context).gateway


@contextmanager
def use_client(client: Any) -> Generator[Any]:
    """Route every SDK call in the block through *client* — a FastAPI `TestClient` for an
    in-process gateway, or any `httpx.Client` — then put back whichever client was there before."""
    with ava.context.clients.using_gateway(client):
        yield client


# Gateway cold start ~0.6s (measured). Default 3 retries, base interval 1s —
# bounded exponential backoff 1s → 2s → 4s → 8s cap (see `_retry_delay_seconds`),
# a much larger window than the restart time and well within the 10s timeout.
# env override: `AVA_GATEWAY_MAX_RETRIES` / `AVA_GATEWAY_RETRY_DELAY_SECONDS`.


def _max_retries() -> int:
    return settings.gateway.gateway_client_max_retries


def _base_retry_delay_s() -> float:
    return settings.gateway.gateway_client_retry_delay_seconds


# ── Transient-failure retry policy ──
# The gateway response policy lives in base.agents.messages.delivery.retry.
# Transport retry and SDK/CLI outbox interception share its known statuses
# (429/502/503/504) and respect committed/retryable wire controls.

# Bounded exponential backoff: attempt i sleeps the base delay multiplied by
# the backoff factor raised to i, capped at `_RETRY_MAX_DELAY_S`, plus a
# deterministic per-agent jitter offset (`_agent_jitter_seconds`) that
# de-phases retry waves across the fleet (heartbeat-daemon pattern, see below).
_RETRY_BACKOFF_FACTOR = 2.0
_RETRY_MAX_DELAY_S = 8.0
# Span of the per-agent jitter offset, seconds.
_JITTER_SPAN_S = 5.0

# ── memory search: its own timeout and retry budget ──
# `/api/memory/search` stands apart from the other routes: the gateway gives
# it a server-side deadline (`memory_search_deadline_seconds`, default 15s)
# and answers a *modelled* 503 (reason indexer_unavailable) when it expires —
# or in ~1s when the query-embed gate is congested (task #2003/E). So the
# default SDK budget (20s x 3 attempts + backoff) spends agent time re-sending
# a request whose failure the gateway already answered; on the 2026-08-29
# fleet-wake storm that was the ~27s overhang measured behind search (#2003/A).
# Two attempts (one retry) still ride out a transient blip (the 2026-08-07
# indexer-500 class, task #960) without stacking the default 3; on a
# persistent outage the caller decides how long it is worth.
_MEMORY_SEARCH_MAX_RETRIES = 2
# Per-attempt HTTP timeout must stay ABOVE the gateway's own search deadline:
# the server's wire 503 is the informative failure (IndexerUnavailable), and a
# client ReadTimeout that beats it converts the error into a generic
# GatewayUnavailable — the exact "caller reads out first" case the deadline
# setting's description warns about.
_MEMORY_SEARCH_TIMEOUT_MARGIN_S = 3.0


def _memory_search_timeout() -> httpx.Timeout:  # pyright: ignore[reportUndefinedVariable]
    """Per-attempt HTTP timeout for one memory search: the gateway's own
    search deadline plus the margin, so the server's deadline fires first."""
    import httpx

    budget = settings.services.memory_search_deadline_seconds + _MEMORY_SEARCH_TIMEOUT_MARGIN_S
    return httpx.Timeout(budget)


def _agent_jitter_seconds(context: AvaContext | None = None) -> float:
    """Deterministic per-agent offset in [0, _JITTER_SPAN_S); 0 when no agent id.

    The heartbeat daemon's per-agent due-time jitter pattern
    (services/wake/heartbeat/daemon.py): a correlated gateway outage hits every
    agent at the same moment, and a fleet-wide identical retry schedule
    (1s, 2s, 4s, ...) would re-synchronize the retry waves as each agent
    retries in lockstep. Offsetting every sleep by a stable per-agent amount
    keeps the waves de-phased. Deterministic on the supplied host context or local SDK identity, so an agent
    keeps its own offset across restarts; no identity
    (tests, non-agent callers) → 0 (no offset).
    """
    from ava.sdk_surface.agent_identity import agent_id

    ident = agent_id(context)
    if ident is None:
        return 0.0
    return _JITTER_SPAN_S * (ident % 1000) / 1000.0


def _retry_delay_seconds(attempt: int, context: AvaContext | None = None) -> float:
    """Sleep before retry `attempt` (0-based): bounded exponential backoff
    plus the deterministic per-agent jitter offset."""
    base = min(_base_retry_delay_s() * _RETRY_BACKOFF_FACTOR**attempt, _RETRY_MAX_DELAY_S)
    return base + _agent_jitter_seconds(context)


def raise_from_response(resp: httpx.Response) -> None:  # pyright: ignore[reportUndefinedVariable]
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
        from base.agents import AgentLaunchFailed

        raise AgentLaunchFailed(
            body["detail"],
            agent_id=body.get("agent_id"),
            state=body.get("state"),
            retry_launch_path=body.get("retry_launch_path"),
        )
    raise EXCEPTION_BY_REASON[reason](body["detail"])


def _wire_reason(resp: httpx.Response) -> tuple[ErrorReason, dict[str, Any]] | None:  # pyright: ignore[reportUndefinedVariable]
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
        body = resp.json()
    except (_json.JSONDecodeError, httpx.DecodingError):
        # JSONDecodeError: body is not JSON (FastAPI default 500 text, ...).
        # DecodingError: httpx 0.28.1 raises it when body decoding fails on a
        # corrupted Content-Encoding (broken gzip/br stream) — same
        # protocol-mismatch class; falls through to the clean HTTP error
        # instead of escaping and masking the status code.
        return None
    if not isinstance(body, dict):
        return None
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

    def __init__(self, response: httpx.Response) -> None:  # pyright: ignore[reportUndefinedVariable]
        self.response = response


def _request_with_retry(
    request: Callable[[], httpx.Response],  # pyright: ignore[reportUndefinedVariable]
    attempts: int,
    *,
    retryable: bool = True,
    context: AvaContext | None = None,
) -> httpx.Response:  # pyright: ignore[reportUndefinedVariable]
    """Execute one route's policy while keeping its final wire response intact."""
    import httpx

    pre_send_errors = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    gateway_classifier = http_classifier.with_(permanent={500})

    def classify(exc: Exception) -> bool:
        if not retryable:
            # A non-idempotent POST can only be repeated when nothing was sent.
            return isinstance(exc, pre_send_errors)
        if isinstance(exc, httpx.TransportError) and not isinstance(exc, NETWORK_ERRORS):
            return False
        return gateway_classifier(exc)

    def once() -> httpx.Response:
        resp = request()
        if retryable and retryable_response(resp):
            raise _TransientResponseError(resp)
        return resp

    if attempts < 1:
        # Preserve the old range(attempts) behavior for a zero/negative override.
        raise GatewayUnavailable(
            f"Gateway transport error at {_http(context).base_url} (after {attempts} retries): None"
        )
    policy = Policy(
        max_attempts=attempts,
        backoff=lambda attempt: _retry_delay_seconds(attempt, context),
        jitter="none",  # backoff already includes the exact agent-only phase
        classify=classify,
        respect_retry_after=False,
    )
    try:
        return retry(policy)(once)
    except _TransientResponseError as exc:
        # The caller still owns raise_from_response and its wire-reason mapping.
        return exc.response
    except NETWORK_ERRORS as exc:
        if not retryable and not isinstance(exc, pre_send_errors):
            raise GatewayUnavailable(
                f"Gateway transport error at {_http(context).base_url} "
                f"(no retry: non-idempotent request; result unknown, may have been delivered): {exc!s}"
            ) from exc
        raise GatewayUnavailable(
            f"Gateway transport error at {_http(context).base_url} (after {attempts} retries): {exc!s}"
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
    timeout: httpx.Timeout | None = None,  # pyright: ignore[reportUndefinedVariable]
    *,
    idempotent: bool | None = None,
    idempotency_key: str | None = None,
    idempotency_scope: str | None = None,
    max_retries: int | None = None,
    context: AvaContext | None = None,
) -> httpx.Response:  # pyright: ignore[reportUndefinedVariable]
    """Unified POST wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Retries known network failures and HTTP 429/502/503/504 responses only when the route
    is idempotent or promises server-side deduplication. For non-idempotent
    routes, only ConnectError, ConnectTimeout, and PoolTimeout are retried:
    these happen before the request can be sent. Other transport failures
    leave the result unknown and must not cause a second POST. Retries use
    bounded exponential backoff + per-agent jitter
    (`_retry_delay_seconds`), then either raises `GatewayUnavailable`
    (transport) or returns the last response (HTTP: the caller's
    `raise_from_response` produces the wire error / `HTTPStatusError` — the
    loud failure carries the full status + body).

    `idempotent=None` (default) inherits the route's semantics from its
    doorplate (`base.api_contracts.contracts`): IDEMPOTENT retries the transient family,
    NON_IDEMPOTENT surfaces an uncertain transport failure immediately (the
    server may have acted on it — re-sending can duplicate the effect, e.g.
    spawn's phantom-twin agent), and
    AT_LEAST_ONCE_WITH_KEY retries with an `Idempotency-Key` header (one key
    per logical call, shared by all retries) — the server dedups, so
    re-sending is safe only for routes protected by the rollout baseline.
    Newly protected keyed routes still send a key, but do not retry ambiguous
    outcomes until positive server capability negotiation is available: an
    older gateway may ignore the key. An explicit `idempotent=...` overrides the contract
    (kept for callers that know better); `idempotency_key` pins the key for
    a caller that wants to control it. The connect family is always
    retried: those fail before anything reached the server.

    `timeout` overrides the per-client default for this single request;
    callers that expect a larger body or server-side work (e.g. send_message
    with a long message body) pass a per-call timeout. `None` means "use the
    client's configured timeout" — there is deliberately no way to ask for an
    unbounded request.

    `idempotency_scope` opts into the principal-v1 wire namespace and requires
    an explicit key on a keyed route. It changes neither the route nor its retry gate.

    `max_retries` overrides the module-wide attempt count (`_max_retries()`) for
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
    if idempotency_scope is not None:
        if not isinstance(idempotency_scope, str) or idempotency_scope != PRINCIPAL_SCOPE:
            raise ValueError("unsupported idempotency scope; expected principal-v1")
        idempotency_key = validate_idempotency_key(idempotency_key)
    per_call = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout

    # Inherit retry semantics from the route's doorplate unless the caller
    # overrides: server promises (base/api_contracts/contracts.py), clients inherit.
    semantics = _post_semantics(path, idempotent=idempotent)
    contract = contracts.contract_for("POST", path)
    retryable = semantics is Idempotency.IDEMPOTENT or (
        semantics is Idempotency.AT_LEAST_ONCE_WITH_KEY
        and contract is not None
        and contract.legacy_keyed_retry
    )
    # One key per logical call — every retry of this call shares it, so the
    # server can dedup the retries against the original.
    key = idempotency_key or _uuid.uuid4().hex
    headers = {"Idempotency-Key": key} if semantics is Idempotency.AT_LEAST_ONCE_WITH_KEY else None

    if idempotency_scope is not None:
        if headers is None:
            raise ValueError("idempotency scope requires a keyed route")
        headers[SCOPE_HEADER] = idempotency_scope

    retries = _max_retries() if max_retries is None else max_retries
    return _request_with_retry(
        lambda: _http(context).post(
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
    timeout: httpx.Timeout | None = None,  # pyright: ignore[reportUndefinedVariable]
    max_retries: int | None = None,
    context: AvaContext | None = None,
) -> httpx.Response:  # pyright: ignore[reportUndefinedVariable]
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

    per_call = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
    retries = _max_retries() if max_retries is None else max_retries
    return _request_with_retry(
        lambda: _http(context).get(path, params=params, timeout=per_call), retries, context=context
    )


def patch(
    path: str, json: dict[str, Any] | None = None, *, idempotency_key: str | None = None
) -> httpx.Response:  # pyright: ignore[reportUndefinedVariable]
    """Unified PATCH wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Retry ambiguous failures only when the route declares natural idempotency.
    A PATCH verb alone does not prove its business effects are repeatable.
    Newly keyed routes send a stable key but do not retry ambiguous failures:
    an older gateway may ignore the header. Pass the original key to replay.
    """
    contract = contracts.contract_for("PATCH", path)
    keyed = contract is not None and contract.idempotency is Idempotency.AT_LEAST_ONCE_WITH_KEY
    key = (
        validate_idempotency_key(idempotency_key)
        if idempotency_key is not None
        else _uuid.uuid4().hex
    )
    headers = {"Idempotency-Key": key} if keyed else None
    return _request_with_retry(
        lambda: (
            _http().patch(path, json=json or {}, headers=headers)
            if keyed
            else _http().patch(path, json=json or {})
        ),
        _max_retries(),
        retryable=contracts.idempotency_for("PATCH", path) is Idempotency.IDEMPOTENT,
    )


def _delete(path: str) -> httpx.Response:  # pyright: ignore[reportUndefinedVariable]
    """Delete with the route's verified retry policy, not a verb-based assumption."""
    return _request_with_retry(
        lambda: _http().delete(path),
        _max_retries(),
        retryable=contracts.idempotency_for("DELETE", path) is Idempotency.IDEMPOTENT,
    )
