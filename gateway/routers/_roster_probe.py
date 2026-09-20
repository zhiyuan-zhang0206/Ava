"""Bounded transport policy for the status roster's runner probes.

One dial's budget, the per-machine failure backoff that spaces re-dials to a
down host, and the fast-fail budget a known-failed host's re-probe gets
(task #3507) live here, beside the dispatch they bound. Split out of
``routers/status.py`` (which sits at the 800-line hard ceiling).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ops import cluster_rpc
from shared.config import settings


async def dispatch_status_probe(
    name: str, ops_url: str, *, timeout_s: float | None = None
) -> dict[str, Any]:
    """Retry one fast transport failure inside the existing total deadline.

    The outer deadline is load-bearing: ``cluster_rpc`` applies ``timeout_s``
    per attempt, so retrying without it could double an 8-second roster budget
    for a blackholed host.

    ``timeout_s`` defaults to the full per-machine budget
    (``settings.gateway.status_probe_timeout_seconds``); callers pass
    ``_probe_budget_s``'s fast-fail budget for a machine that already carries
    reachability failures.
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
# host is re-probed on an exponential schedule: min(base * 2**failures, cap)
# seconds, with the two bounds in config
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
