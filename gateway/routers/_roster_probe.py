"""Bounded transport policy for the status roster's runner probes.

One dial's budget, the per-machine failure backoff that spaces re-dials to a
down host, and the detached single-flight recovery dial a known-down host's
recovery check runs on (task #3507) live here, beside the dispatch they bound.
Split out of ``routers/status.py`` (which sits at the 800-line hard ceiling).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from ops import cluster_rpc
from shared.config import settings

_log = logging.getLogger(__name__)


async def dispatch_status_probe(
    name: str, ops_url: str, *, timeout_s: float | None = None
) -> dict[str, Any]:
    """Retry one fast transport failure inside the existing total deadline.

    The outer deadline is load-bearing: ``cluster_rpc`` applies ``timeout_s``
    per attempt, so retrying without it could double an 8-second roster budget
    for a blackholed host.

    ``timeout_s`` defaults to the full per-machine budget
    (``settings.gateway.status_probe_timeout_seconds``); the detached recovery
    dial passes ``_probe_budget_s``'s fast-fail budget for a machine that
    already carries reachability failures.
    """
    if timeout_s is None:
        timeout_s = settings.gateway.status_probe_timeout_seconds
    try:
        async with asyncio.timeout(timeout_s):
            return await cluster_rpc.dispatch_to_machine(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=timeout_s,
                ops_url=ops_url,
                retries=1,
            )
    except TimeoutError as exc:
        raise cluster_rpc.ClusterOpUnreachable(
            f"status_probe for machine={name!r} exceeded its {timeout_s:.1f}s total budget"
        ) from exc


# Per-machine probe backoff. A machine that keeps failing its status_probe (a
# down host — e.g. a flaky WSL peer) would otherwise be dialed on every panel
# poll (~5s), one wasted round-trip + one log line each. Instead a recently-failed
# host's detached recovery dial runs on an exponential schedule: min(base * 2**failures,
# cap) seconds, with the two bounds in config
# (gateway.status_probe_backoff_base_seconds / _cap_seconds, resolved at call
# time; the schedule literals moved there under the numeric-limits convention,
# tasks #3507 / #3696). Only reachability failures (ClusterOpUnreachable) widen
# the window; any reachable answer (a probe success, or an op-level
# ClusterOpFailed — the host responded) clears it back to the normal cadence.
# State is process-local monotonic time; a gateway restart drops it, which just
# re-probes everyone once and rebuilds the schedule. Concurrent panel polls
# (sync handler, threadpool) may race on this dict, but the ops are GIL-atomic
# and the worst case is one redundant probe or an off-by-one failure count —
# acceptable for a diagnostic throttle.
_probe_failures: dict[str, tuple[int, float]] = {}  # name -> (consecutive_failures, last_attempt)


def _backoff_window_s(failures: int) -> float:
    """The current re-probe window for `failures` consecutive unreachable probes."""
    return min(
        settings.gateway.status_probe_backoff_base_seconds * (2**failures),
        settings.gateway.status_probe_backoff_cap_seconds,
    )


def _probe_in_backoff(name: str) -> bool:
    """True when `name` failed recently enough that its next probe is still
    deferred. A name with no failure record is never deferred (normal cadence)."""
    state = _probe_failures.get(name)
    if state is None:
        return False
    failures, last_attempt = state
    return (time.monotonic() - last_attempt) < _backoff_window_s(failures)


def _note_probe_unreachable(name: str) -> None:
    """Record an unreachable probe: bump the consecutive-failure count and stamp
    the attempt, widening the next backoff window."""
    failures = _probe_failures.get(name, (0, 0.0))[0]
    _probe_failures[name] = (failures + 1, time.monotonic())


def _note_probe_reachable(name: str) -> None:
    """Clear any backoff for `name` — it answered, so resume the normal cadence."""
    _probe_failures.pop(name, None)


def _probe_budget_s(name: str, *, full_budget_s: float | None = None) -> float:
    """The status_probe budget for `name`'s next dial.

    A machine that already carries reachability failures gets the fast-fail
    budget: its re-dial exists only to notice recovery, and a blackholed
    re-dial allowed to burn the full budget drags the whole-table roster read
    past the CLI/UI read budget — the 2026-09-15 offline transition showed the
    read timing out whenever a re-dial hung (task #3507, 5/18 samples). First
    contact with a not-yet-failed machine keeps the full budget: that margin is
    what stops a slow-but-healthy host from reading offline (#1200).
    """
    if name in _probe_failures:
        return settings.gateway.status_probe_fastfail_timeout_seconds
    if full_budget_s is not None:
        # A heavier op than a status probe (the inventory aggregate) brings its
        # own wider first-contact budget; the fast-fail side stays shared.
        return full_budget_s
    return settings.gateway.status_probe_timeout_seconds


# A known-down host is never dialed on a read path (task #3507): the read serves
# the cached offline row, and the recovery check — the dial that can notice the
# host came back — runs detached in its own daemon thread, paced by the same
# failure backoff the inline re-dial used to be. The whole-table read therefore
# never carries a down host's dial budget, and the recovery cadence is
# unchanged: one in-flight dial per host, its outcome folded back into the state
# below.
_recovery_inflight: set[str] = set()
_recovery_lock = threading.Lock()


def _start_recovery_thread(name: str, ops_url: str) -> None:
    """Spawn the detached recovery dial (the test seam for thread behavior)."""
    threading.Thread(
        target=_run_recovery_dial,
        args=(name, ops_url),
        name=f"roster-recovery-{name}",
        daemon=True,
    ).start()


def _run_recovery_dial(name: str, ops_url: str) -> None:
    """One detached recovery dial for a known-down host.

    Runs the dispatch the read path used to run inline, under
    ``_probe_budget_s`` (the fast-fail budget while the failure record stands),
    then folds the outcome into the shared state exactly as the inline re-dial
    did: a reachable answer clears the record (the next read dials the host
    fresh as a first contact), an unreachable one widens the window, and an
    unexpected failure leaves both untouched so the next window retries.
    Bounded by the dial budget — the thread lives at most that long and always
    converges back into this state machine.
    """
    reachable: bool | None = None
    try:
        asyncio.run(dispatch_status_probe(name, ops_url, timeout_s=_probe_budget_s(name)))
        reachable = True
    except cluster_rpc.ClusterOpUnreachable:
        reachable = False
    except cluster_rpc.ClusterOpFailed:
        # The ops server answered and its op raised — the host is reachable.
        reachable = True
    except Exception:
        _log.exception("detached recovery dial for %r failed unexpectedly", name)
    # Fold the outcome in before releasing the in-flight slot: a read racing
    # this completion must see the final state, never (slot-free + stale record).
    if reachable is True:
        _note_probe_reachable(name)
    elif reachable is False:
        _note_probe_unreachable(name)
    _recovery_inflight.discard(name)


def _maybe_kick_recovery_dial(name: str, ops_url: str | None) -> None:
    """Start `name`'s detached recovery dial when one is due.

    Single-flight per machine: at most one recovery dial may be in flight, and
    only once the failure window has elapsed (the same
    ``min(base * 2**failures, cap)`` schedule the inline re-dial used) — a read
    inside the window, or during an in-flight dial, just serves the cached row.
    A host with no advertised address has nothing to dial. Best-effort by
    contract: failing to spawn must never fail the read it was kicked from.
    """
    if ops_url is None or name not in _probe_failures or _probe_in_backoff(name):
        return
    with _recovery_lock:
        if name in _recovery_inflight:
            return
        _recovery_inflight.add(name)
    _start_recovery_thread(name, ops_url)


# Identity-mismatch episode tracking: one log line per mismatching episode, not
# one per panel poll. A stopped row's stale URL answering as a different host is
# the expected face of the stop (its address was handed on); an active row's
# mismatch is the real misregistration signal (2026-07-18 / 2026-08-30). The
# episode ends once a probe's identity echoes correctly again, so a later
# mismatch logs anew. Process-local like the probe backoff: a gateway restart
# just re-logs once.
_identity_mismatch_active: set[str] = set()


def log_identity_mismatch(
    name: str, gateway_url: str | None, responder: str, *, stopped: bool
) -> None:
    """Log one identity-mismatch sighting, deduped to once per episode.

    `stopped` (the machines row carries a stop marker) downgrades the line to
    INFO and says why the mismatch is expected — reported once, not at panel
    cadence. An active row keeps the loud ERROR: a loopback / misregistered
    gateway_url that makes the gateway dial itself and answer under its own
    name is exactly the 2026-07-18 incident class and must not go quiet.
    """
    first_of_episode = name not in _identity_mismatch_active
    _identity_mismatch_active.add(name)
    if not first_of_episode:
        return
    if stopped:
        _log.info(
            "identity mismatch on stopped machine %r at %s: the ops server there "
            "self-reported %r — the row's stale URL answers for another host; "
            "reported once until the identity echoes correctly again",
            name,
            gateway_url,
            responder,
        )
    else:
        _log.error(
            "identity mismatch: probing machine %r at %s, but the ops server self-reported %r",
            name,
            gateway_url,
            responder,
        )


def note_identity_match(name: str) -> None:
    """End `name`'s mismatch episode — its identity echoed correctly again."""
    _identity_mismatch_active.discard(name)
