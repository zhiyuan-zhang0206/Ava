"""HTTP transport and wire-error translation for the gateway SDK client."""

from __future__ import annotations

import json as _json
import time as _time
import uuid as _uuid

import ava
from shared import contracts
from shared.agents import EXCEPTION_BY_REASON, ErrorReason, GatewayUnavailable
from shared.cluster_auth import bearer_header
from shared.config import settings
from shared.contracts import Idempotency

# Singleton: process-wide shared connection pool. Connect/read timeout is a
# guard against a stuck gateway line. Most gateway ops are near-instant,
# but spawn does real server-side work (launch the new process + poll until it
# claims its row), so the budget must comfortably exceed the server's confirm
# window — otherwise a read timeout on a spawn that DID succeed triggers a retry
# that re-POSTs the non-idempotent create and yields a phantom-twin agent.
# Default value see `shared.config.Settings.gateway_client_http_timeout_seconds`;
# env override: `AVA_GATEWAY_HTTP_TIMEOUT_SECONDS`.
_client: httpx.Client | None = None  # noqa: F821  # pyright: ignore[reportUndefinedVariable]


def _client_singleton() -> httpx.Client:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """Process-wide httpx client, built on first use so importing the SDK in a
    no-config context does not require the gateway URL to be resolvable until an
    actual call is made. Tests may inject a client by setting the module global
    `_client` directly (e.g. a FastAPI TestClient)."""
    import httpx

    from shared.http_dial import transport_for_url

    global _client  # noqa: PLW0603 — lazy module singleton
    if _client is None:
        # The gateway requires auth on every API route (always-on cluster
        # secret). The SDK is a script/agent caller, so it presents the secret
        # as `Authorization: Bearer <secret>` (the cookie path is the browser's).
        # settings.data_plane.cluster_secret is sourced from the cluster .env even when the
        # agent process env lacks it. Empty secret (tests / unprovisioned
        # checkout) sends no header — matches the gateway's fail-open when its
        # own secret is unset.
        headers = (
            bearer_header(settings.data_plane.cluster_secret)
            if settings.data_plane.cluster_secret
            else {}
        )
        _client = httpx.Client(
            base_url=ava.GATEWAY_URL,
            timeout=httpx.Timeout(settings.gateway.gateway_client_http_timeout_seconds),
            headers=headers,
            # Pins the dial when GATEWAY_URL's host is an IPv4 literal (e.g. a
            # private-network address) — see shared/http_dial.py. None (a hostname
            # target) is httpx's own default transport, unchanged.
            transport=transport_for_url(ava.GATEWAY_URL),
        )
    return _client


# Gateway cold start ~0.6s (measured). Default 3 retries, base interval 1s —
# bounded exponential backoff 1s → 2s → 4s → 8s cap (see `_retry_delay_seconds`),
# a much larger window than the restart time and well within the 10s timeout.
# env override: `AVA_GATEWAY_MAX_RETRIES` / `AVA_GATEWAY_RETRY_DELAY_SECONDS`.
_MAX_RETRIES = settings.gateway.gateway_client_max_retries
_RETRY_DELAY_S = settings.gateway.gateway_client_retry_delay_seconds

# ── Transient-failure retry policy ──
# HTTP statuses that mean "the gateway or one of its backends hiccuped" —
# worth re-sending an idempotent request. 500 = unhandled server error (may be
# a transient blip — the 2026-08-07 memory-indexer 500 class that crashed an
# agent's graph before this policy), 502/503 = a backend (indexer / cross-
# machine runner) is down, 504 = gateway-side timeout, 429 = rate-limited.
# 4xx are NOT here: the wire `reason` is authoritative application semantics
# (AgentNotFound etc.); retrying cannot change the result.
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

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


def _memory_search_timeout() -> httpx.Timeout:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """Per-attempt HTTP timeout for one memory search: the gateway's own
    search deadline plus the margin, so the server's deadline fires first."""
    import httpx

    budget = settings.services.memory_search_deadline_seconds + _MEMORY_SEARCH_TIMEOUT_MARGIN_S
    return httpx.Timeout(budget)


def _agent_jitter_seconds() -> float:
    """Deterministic per-agent offset in [0, _JITTER_SPAN_S); 0 when no agent id.

    The heartbeat daemon's per-agent due-time jitter pattern
    (services/heartbeat/daemon.py): a correlated gateway outage hits every
    agent at the same moment, and a fleet-wide identical retry schedule
    (1s, 2s, 4s, ...) would re-synchronize the retry waves as each agent
    retries in lockstep. Offsetting every sleep by a stable per-agent amount
    keeps the waves de-phased. Deterministic on the agent id (the turn
    contextvar in the host, or AVA_AGENT_ID carried by a launched child) so an agent keeps its own offset across restarts; no identity
    (tests, non-agent callers) → 0 (no offset).
    """
    from shared.turn_identity import effective_agent_id

    ident = effective_agent_id()
    if ident is None:
        return 0.0
    return _JITTER_SPAN_S * (ident % 1000) / 1000.0


def _retry_delay_seconds(attempt: int) -> float:
    """Sleep before retry `attempt` (0-based): bounded exponential backoff
    plus the deterministic per-agent jitter offset."""
    base = min(_RETRY_DELAY_S * _RETRY_BACKOFF_FACTOR**attempt, _RETRY_MAX_DELAY_S)
    return base + _agent_jitter_seconds()


def _raise_from_response(resp: httpx.Response) -> None:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
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
    raise EXCEPTION_BY_REASON[reason](body["detail"])


def _wire_reason(resp: httpx.Response) -> tuple[ErrorReason, dict] | None:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
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


def _post(
    path: str,
    json: dict | None = None,
    params: dict | None = None,
    timeout: httpx.Timeout | None = None,  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    *,
    idempotent: bool | None = None,
    idempotency_key: str | None = None,
    max_retries: int | None = None,
) -> httpx.Response:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """Unified POST wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Retries the transient failure family — the `httpx.TransportError` parent
    class (ConnectError / ConnectTimeout / ReadTimeout / WriteTimeout /
    PoolTimeout / RemoteProtocolError etc.: "the gateway line is not
    reachable", which subclass is a transport-layer detail) plus HTTP
    429/5xx responses (`_TRANSIENT_HTTP_STATUSES`: the gateway or one of its
    backends hiccuped) — with bounded exponential backoff + per-agent jitter
    (`_retry_delay_seconds`), then either raises `GatewayUnavailable`
    (transport) or returns the last response (HTTP: the caller's
    `_raise_from_response` produces the wire error / `HTTPStatusError` — the
    loud failure carries the full status + body).

    `idempotent=None` (default) inherits the route's semantics from its
    doorplate (`shared.contracts`): IDEMPOTENT retries the transient family,
    NON_IDEMPOTENT surfaces immediately (a ReadTimeout means the request
    left this process and the server may well have acted on it — re-sending
    can duplicate the effect, e.g. spawn's phantom-twin agent), and
    AT_LEAST_ONCE_WITH_KEY retries with an `Idempotency-Key` header (one key
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

    `max_retries` overrides the module-wide attempt count (`_MAX_RETRIES`) for
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
    per_call = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout

    # Inherit retry semantics from the route's doorplate unless the caller
    # overrides: server promises (shared/contracts.py), clients inherit.
    semantics = (
        Idempotency.IDEMPOTENT
        if idempotent
        else Idempotency.NON_IDEMPOTENT
        if idempotent is not None
        else contracts.idempotency_for("POST", path)
    )
    retryable = semantics is not Idempotency.NON_IDEMPOTENT
    # One key per logical call — every retry of this call shares it, so the
    # server can dedup the retries against the original.
    key = idempotency_key or _uuid.uuid4().hex
    headers = {"Idempotency-Key": key} if semantics is Idempotency.AT_LEAST_ONCE_WITH_KEY else None

    last_err: Exception | None = None
    retries = _MAX_RETRIES if max_retries is None else max_retries
    for attempt in range(retries):
        try:
            resp = _client_singleton().post(
                path, json=json or {}, params=params, timeout=per_call, headers=headers
            )
        except httpx.TransportError as e:
            if isinstance(e, httpx.ReadTimeout) and not retryable:
                # The request may have landed (non-idempotent POST): fail now
                # rather than re-send and double the effect (spawn → twin).
                raise GatewayUnavailable(
                    f"Gateway read timeout at {_client_singleton().base_url} "
                    f"(no retry: non-idempotent request): {e!s}"
                ) from e
            last_err = e
        else:
            if resp.status_code in _TRANSIENT_HTTP_STATUSES and retryable and attempt < retries - 1:
                # Backend blip; the request is safe to re-send (idempotent,
                # or AtLeastOnceWithKey — the server dedups by our key).
                # Record the response and retry — the final failure (if
                # retries run out) is still loud: the last response is
                # returned so `_raise_from_response` surfaces the wire error /
                # HTTPStatusError with the full status + body.
                last_err = httpx.HTTPStatusError(
                    f"transient HTTP {resp.status_code} for POST {path}",
                    request=resp.request,
                    response=resp,
                )
            else:
                return resp
        if attempt < retries - 1:
            _time.sleep(_retry_delay_seconds(attempt))
    raise GatewayUnavailable(
        f"Gateway transport error at {_client_singleton().base_url} (after {retries} retries): {last_err!s}"
    ) from last_err


def _get(path: str, *, params: dict | None = None) -> httpx.Response:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """Unified GET wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Same policy as `_post`; a GET is always idempotent, so transient HTTP
    429/5xx responses are retried too.
    """
    import httpx

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _client_singleton().get(path, params=params)
        except httpx.TransportError as e:
            last_err = e
        else:
            if resp.status_code in _TRANSIENT_HTTP_STATUSES and attempt < _MAX_RETRIES - 1:
                last_err = httpx.HTTPStatusError(
                    f"transient HTTP {resp.status_code} for GET {path}",
                    request=resp.request,
                    response=resp,
                )
            else:
                return resp
        if attempt < _MAX_RETRIES - 1:
            _time.sleep(_retry_delay_seconds(attempt))
    raise GatewayUnavailable(
        f"Gateway transport error at {_client_singleton().base_url} (after {_MAX_RETRIES} retries): {last_err!s}"
    ) from last_err


def _patch(path: str, json: dict | None = None) -> httpx.Response:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """Unified PATCH wrapper + transient-failure retry + failure → GatewayUnavailable conversion.

    Same policy as `_get` — a PATCH (partial update, e.g. edit the current
    notice) is idempotent by contract: repeating it cannot change the
    outcome beyond the first application.
    """
    import httpx

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _client_singleton().patch(path, json=json or {})
        except httpx.TransportError as e:
            last_err = e
        else:
            if resp.status_code in _TRANSIENT_HTTP_STATUSES and attempt < _MAX_RETRIES - 1:
                last_err = httpx.HTTPStatusError(
                    f"transient HTTP {resp.status_code} for PATCH {path}",
                    request=resp.request,
                    response=resp,
                )
            else:
                return resp
        if attempt < _MAX_RETRIES - 1:
            _time.sleep(_retry_delay_seconds(attempt))
    raise GatewayUnavailable(
        f"Gateway transport error at {_client_singleton().base_url} (after {_MAX_RETRIES} retries): {last_err!s}"
    ) from last_err


def _delete(path: str) -> httpx.Response:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """Unified DELETE wrapper + transient-failure retry + failure → GatewayUnavailable. Same policy as `_post`; DELETE is idempotent by semantics."""
    import httpx

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _client_singleton().delete(path)
        except httpx.TransportError as e:
            last_err = e
        else:
            if resp.status_code in _TRANSIENT_HTTP_STATUSES and attempt < _MAX_RETRIES - 1:
                last_err = httpx.HTTPStatusError(
                    f"transient HTTP {resp.status_code} for DELETE {path}",
                    request=resp.request,
                    response=resp,
                )
            else:
                return resp
        if attempt < _MAX_RETRIES - 1:
            _time.sleep(_retry_delay_seconds(attempt))
    raise GatewayUnavailable(
        f"Gateway transport error at {_client_singleton().base_url} (after {_MAX_RETRIES} retries): {last_err!s}"
    ) from last_err
