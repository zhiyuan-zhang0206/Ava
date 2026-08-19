"""Cluster RPC — gateway -> agent-runner direct request/response.

The gateway runs a cluster op on a remote agent-runner by POSTing to that
host's ava-ops server (`services/agent_ops`). The host's reachable address is
read from the `machines` table (each host registers its ops URL at `ava start`).
One synchronous round-trip with bounded retry; no queue, no SSE — the cluster's
private network makes every node mutually dialable, so a control op is a single
HTTP call.

  dispatch_to_machine(target_machine, kind, payload, ...) -> result dict
      POST {ops_url}/ops {"kind", "payload", "idempotency_key"?} -> {"status", "result"}.
      Returns result on status=="completed"; raises ClusterOpFailed on
      status=="failed", ClusterOpUnreachable on connect/timeout/non-200.

  Retry policy: transient infrastructure failures (transport errors, 5xx)
  retry with bounded exponential backoff + jitter (see `_retry_delay_s`).
  Business failures (status=="failed") and deterministic rejections (4xx,
  malformed body, unknown machine) never retry. An op whose effect is NOT
  repeatable — spawn (a second run creates a twin agent), cluster_update (a
  second run spawns a second updater), lifecycle terminate/restart (a second
  run inserts a second inbound) — is safe to retry only because this module
  attaches an idempotency key to the envelope: the ops server dedupes by key
  and replays the first run's stored outcome instead of re-executing, so a
  lost response cannot duplicate the effect (see
  `services/agent_ops/daemon.py:_dispatch_idempotent`). The key is a fresh
  UUID per dispatch unless the caller supplies one (`idempotency_key`) for
  cross-call dedup.

The agent-runner side that serves /ops lives in `services/agent_ops/daemon.py`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Any

import httpx
from pydantic import ValidationError

from ops.rpc_schemas import OpEnvelope, OpKind, OpResponse
from shared.cluster_auth import bearer_header
from shared.config import settings
from shared.machines import (
    MachineGatewayUrlMissing,
    MachineNotRegistered,
)
from shared.machines import (
    lookup as lookup_machine_url,
)

__all__ = [
    "ClusterOpFailed",
    "ClusterOpUnreachable",
    "OpKind",
    "dispatch_to_machine",
]

_log = logging.getLogger(__name__)

# OpKind (the op vocabulary) now lives in ops.rpc_schemas beside the wire models;
# re-exported here (imported above, listed in __all__) so existing importers of
# `ops.cluster_rpc.OpKind` keep working.

# ── Retry policy ─────────────────────────────────────────────────────────────
# A dispatch is one synchronous round-trip over the cluster's private network,
# and it used to be single-shot: one transport hiccup (the 2026-08-06 fleet
# transient) dropped a cross-machine op outright. Load-bearing ops now retry on
# transient infrastructure failures with bounded exponential backoff + jitter.
# Two bounds keep the retry cheap:
#   * `retries` caps the extra attempts (default AVA_CLUSTER_RPC_MAX_RETRIES,
#     3 -> 4 attempts total; worst-case wall for a blackholed host ~4x
#     timeout_s, which latency-critical callers already shorten per-op);
#   * the per-attempt delay is capped (_RETRY_MAX_DELAY_S) and jittered ±50% so
#     a fan-out whose targets all failed together (a rollout pausing N runners
#     at once) does not re-dial them in lockstep.
_RETRY_BASE_DELAY_S = 0.5
_RETRY_MAX_DELAY_S = 4.0

# Op kinds whose effect is NOT repeatable — re-executing a lost op duplicates
# the effect (a second launch for spawn-launch, a second ava-updater session for
# cluster_update, a second terminate/restart inbound for lifecycle). These are
# retried only under an idempotency key (auto-generated here unless the caller
# supplies one); the ops server dedupes by key, so a retry replays the first
# run's stored outcome instead of executing a second time.
_NON_IDEMPOTENT_KINDS = frozenset({"spawn-launch", "cluster_update", "lifecycle"})

# Module-level alias so tests can pin the retry sleep without patching asyncio.
_sleep = asyncio.sleep


def _retry_delay_s(attempt: int) -> float:
    """Bounded exponential backoff for retry `attempt` (0-based), jittered ±50%.

    base = min(0.5 * 2**attempt, 4.0) seconds — 0.5, 1, 2, then capped at 4.
    The ±50% jitter de-synchronizes a fan-out whose targets failed together
    (without it, a rollout pausing N runners would re-dial all N in lockstep
    and the retries would land as a synchronized burst).
    """
    base = min(_RETRY_BASE_DELAY_S * (2**attempt), _RETRY_MAX_DELAY_S)
    # S311: jitter needs spread, not secrecy — stdlib random is right (same
    # ruling as shared/lm/_call.py's retry jitter).
    return base * random.uniform(0.5, 1.5)  # noqa: S311


class _RetryableFailure(Exception):  # noqa: N818 — internal retry signal, not a caller-facing error class
    """One dispatch attempt failed for a transient infrastructure reason
    (transport error / 5xx) — the bounded backoff loop in `dispatch_to_machine`
    retries it. Internal to this module; never escapes to callers (the loop
    converts the last failure into `ClusterOpUnreachable`)."""

    def __init__(self, cause: Exception, *, url: str) -> None:
        super().__init__(f"{url}: {cause!r}")
        self.cause = cause
        self.url = url


class ClusterOpUnreachable(RuntimeError):  # noqa: N818 — state description; same style as TimeoutError
    """The target agent-runner's ops server could not be reached or did not respond.

    Covers a missing/NULL machines row, a connect/read timeout, a non-200
    status from /ops (after retries, for transient statuses), and a malformed
    response body. Distinct from ClusterOpFailed (a reached host whose op
    raised a business error).
    """


class ClusterOpFailed(RuntimeError):  # noqa: N818 — semantic predicate, not an error class
    """The target agent-runner reached its op but the op reported failure.

    `result` carries the failure payload ({"error", optionally "detail" +
    "reason"}); the caller re-emits the original wire exception from it.
    """

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(f"cluster op failed: {result}")
        self.result = result


async def _dispatch_once(
    target_machine: str,
    kind: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    ops_url: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """One POST round-trip to `{ops_url}/ops`; returns the op's result dict.

    Raises:
        ClusterOpUnreachable: terminal conditions — the host has no advertised
            address (handled by the caller), a non-5xx non-200 status, or a
            malformed response body (version skew).
        ClusterOpFailed: the host ran the op but it reported `status=failed`.
        _RetryableFailure: transient infrastructure failure (transport error /
            timeout, or a 5xx status) — safe to retry; the caller's loop owns
            the retry budget.
    """
    url = f"{ops_url.rstrip('/')}/ops"
    started = time.monotonic()
    try:
        # connect is capped by timeout_s: a blackholed host (powered-off private-
        # network peer) hangs in connect, so a connect floor above timeout_s would defeat
        # short probe timeouts like the roster's 3s probe.
        # Present the cluster secret so the runner's authenticated /ops accepts
        # the dial. When the secret is empty (tests, unprovisioned checkout), skip
        # the header entirely to avoid an illegal "Bearer " value.
        secret = settings.data_plane.cluster_secret
        headers = bearer_header(secret) if secret else {}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))
        ) as client:
            resp = await client.post(
                url,
                # exclude_none: an idempotent op's envelope is byte-identical to
                # the pre-#961 wire (no idempotency_key field at all), so old
                # ops servers see exactly what they always saw for those kinds.
                json=OpEnvelope(
                    kind=kind, payload=payload, idempotency_key=idempotency_key
                ).model_dump(exclude_none=True),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise _RetryableFailure(exc, url=url) from exc

    _log.info(
        "cluster_rpc %s -> machine=%s responded %d in %.1fs",
        kind,
        target_machine,
        resp.status_code,
        time.monotonic() - started,
    )
    if resp.status_code != 200:
        if 500 <= resp.status_code < 600:
            # Server-side transient (ops server mid-restart, a proxy hiccup) —
            # retried by the backoff loop. Every other non-200 (400 malformed
            # body, 401 auth, 404 wrong path) is deterministic and fails fast.
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise _RetryableFailure(exc, url=url) from exc
        raise ClusterOpUnreachable(
            f"ops server for machine={target_machine!r} returned {resp.status_code} at {url}: "
            f"{resp.text[:200]}"
        )

    # The wire contract is exactly {status in {completed, failed}, result}; a
    # malformed body or an unknown status (version skew / wrong server) fails the
    # OpResponse validation and is surfaced as unreachable rather than returning a
    # garbage `result` as if the op succeeded.
    try:
        envelope = OpResponse.model_validate(resp.json())
    except ValidationError as exc:
        raise ClusterOpUnreachable(
            f"ops server for machine={target_machine!r} returned a malformed response at {url}: "
            f"{resp.text[:200]}"
        ) from exc
    if envelope.status == "completed":
        return envelope.result
    raise ClusterOpFailed(envelope.result)


async def dispatch_to_machine(
    target_machine: str,
    kind: OpKind,
    payload: dict[str, Any],
    *,
    timeout_s: float | None = None,
    ops_url: str | None = None,
    retries: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """POST one op to `target_machine`'s ops server, block on the response.

    POSTs `{kind, payload}` to `{ops_url}/ops` and returns the op's result dict,
    retrying transient infrastructure failures (transport errors / timeouts and
    5xx statuses) with bounded exponential backoff + jitter (`_retry_delay_s`).
    `retries` caps the extra attempts (default
    `settings.gateway.cluster_rpc_max_retries` / AVA_CLUSTER_RPC_MAX_RETRIES);
    pass 0 for a fail-fast single attempt (e.g. latency-critical status probes,
    which carry their own per-machine backoff). Business failures
    (status=failed), 4xx statuses, malformed responses and an unresolvable
    machines row never retry.

    Non-idempotent kinds (`spawn`, `cluster_update`, `lifecycle`) are retried
    under an idempotency key: auto-generated per dispatch here (a fresh UUID)
    unless the caller passes `idempotency_key` — the ops server dedupes by key,
    so a retry after a lost response replays the first run's stored outcome
    instead of re-executing and duplicating the effect. Callers that dispatch
    the SAME logical op more than once (any future cross-call retry) must pass
    the same explicit key across those calls.

    `timeout_s` defaults to `settings.gateway.cluster_rpc_timeout_seconds`
    (AVA_CLUSTER_RPC_TIMEOUT_SECONDS) when omitted. It bounds each attempt.

    `ops_url` is the host's ops base URL. Pass it pre-resolved (from a
    `machines`-table read done earlier, while Postgres was known up) to make the
    dial Postgres-independent — the compensating `cluster/resume` in a failed
    `ava cluster update` runs after the local update may have taken the data plane down,
    so a lookup here would raise `psycopg.OperationalError` and sink the
    compensation (the 2026-07-20 incident). Omitted (None) → resolve it from the
    `machines` table now (the ordinary path, gateway + Postgres up).

    Raises:
        ClusterOpUnreachable: the host has no advertised address, or the POST
            hit a connect/read timeout or non-200 status (after retries for
            transient statuses).
        ClusterOpFailed: the host ran the op but it reported `status=failed`.
    """
    if timeout_s is None:
        timeout_s = settings.gateway.cluster_rpc_timeout_seconds

    if ops_url is None:
        try:
            ops_url = lookup_machine_url(target_machine)
        except (MachineNotRegistered, MachineGatewayUrlMissing) as exc:
            raise ClusterOpUnreachable(
                f"cannot resolve an address for machine={target_machine!r}: {exc}"
            ) from exc

    # Non-idempotent kinds retry only under an idempotency key (see the module
    # docstring). Auto-generate one per dispatch — it is stable across the
    # attempts of THIS retry loop, which is all the loop needs; a caller that
    # re-dispatches the same logical op across calls passes its own key.
    if kind in _NON_IDEMPOTENT_KINDS and idempotency_key is None:
        idempotency_key = f"{kind}:{uuid.uuid4()}"
    if retries is None:
        retries = settings.gateway.cluster_rpc_max_retries
    retries = max(retries, 0)  # a negative budget means "no attempts" — never

    started = time.monotonic()
    last: _RetryableFailure | None = None
    for attempt in range(retries + 1):
        try:
            return await _dispatch_once(
                target_machine,
                kind,
                payload,
                timeout_s=timeout_s,
                ops_url=ops_url,
                idempotency_key=idempotency_key,
            )
        except _RetryableFailure as exc:
            last = exc
            if attempt < retries:
                delay = _retry_delay_s(attempt)
                # Intermediate failures are DEBUG for every kind: the final
                # outcome (success, or the exhausted WARNING below) is what an
                # operator needs; each intermediate line is retry machinery.
                _log.debug(
                    "cluster_rpc %s -> machine=%s attempt %d/%d failed, retrying in %.1fs: %r",
                    kind,
                    target_machine,
                    attempt + 1,
                    retries + 1,
                    delay,
                    exc.cause,
                )
                await _sleep(delay)
    # Retries exhausted — loud failure for a load-bearing op. status_probe
    # reachability failures stay DEBUG (an offline host at panel cadence is
    # steady-state; the roster's own per-machine backoff widens the gap), every
    # other kind is operator-initiated so its exhausted retry stays a WARNING.
    # `last` is guaranteed set: the loop runs at least once (retries >= 0 is
    # enforced above) and every attempt either returns or raises.
    if last is None:  # pragma: no cover — unreachable by the loop invariant
        raise RuntimeError(
            f"cluster_rpc retry loop exhausted without recording a failure "
            f"(machine={target_machine!r}, kind={kind!r})"
        )
    log = _log.debug if kind == "status_probe" else _log.warning
    log(
        "cluster_rpc %s -> machine=%s unreachable after %d attempt(s) (%.1fs): %r",
        kind,
        target_machine,
        retries + 1,
        time.monotonic() - started,
        last.cause,
    )
    raise ClusterOpUnreachable(
        f"ops server for machine={target_machine!r} unreachable at {last.url} "
        f"after {retries + 1} attempt(s): {last.cause!r}"
    ) from last.cause
