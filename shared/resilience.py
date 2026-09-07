"""Shared retry primitives — the single retry-loop implementation (R2-D).

R2 convergence point D (design-concept.md §4.4 + evaluation-record #14):
- ``Policy`` — immutable retry parameters: max_attempts x exponential backoff
  x jitter (per-process deterministic phase + random ±span, de-phasing the
  whole fleet) x classify x idempotent gate x Retry-After respect.
- ``retry(policy)(fn)`` / ``aretry(policy)(fn)`` — thin executors, agnostic to
  the HTTP client: ``fn`` is any zero-argument callable (urllib / httpx /
  aiohttp / provider SDKs). This module is the DESIGNATED convergence target
  for retry loops (design invariant D1), not yet the only one: dedicated
  loops still live in ``shared/lm/_call.py`` (invoke_text, its own
  exponential backoff + jittered), ``shared/bootstrap.py``
  (fetch_bootstrap_config, linear backoff without jitter) and
  ``shared/redis_listener.py`` (connect retry, backoff without jitter). The
  R2-D migration folds them in; until then every NEW retry loop must be
  built on this module (grep-able).
- ``http_classifier`` — the one error-classification semantics (D2); call
  sites compose local overrides via ``with_(permanent=...)`` /
  ``with_(transient=...)`` instead of re-implementing classification
  (only-override, never-rewrite).
- ``jittered`` / ``extract_retry_after`` — the shared sleep-spread and
  Retry-After helpers every migrated call site uses.

Idempotency gate (D3): with ``idempotent=False`` the call runs exactly once
and a final failure MUST be made visible — ``on_final_failure`` compensation
hook (alert / queue / raise) or the raised exception itself; silent loss is
structurally impossible.

``with_`` carries a trailing underscore because ``with`` is a Python keyword
— the design's ``http_classifier.with(permanent=...)`` spelling is not a
legal method name.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import urllib.error
from collections.abc import Awaitable, Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from shared.turn_identity import effective_agent_id

__all__ = [
    "ExponentialBackoff",
    "Policy",
    "aretry",
    "extract_retry_after",
    "http_classifier",
    "jittered",
    "retry",
]

T = TypeVar("T")

# HTTP statuses whose failure is transient (rate limit / upstream hiccup). A
# deterministic 4xx is a caller/upstream bug, not a retry gap.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# Longest Retry-After honored at this layer (mirrors the provider-SDK cap);
# a longer header is treated as absent rather than blindly slept.
_MAX_RETRY_AFTER_RESPECT_S = 120.0

# Module-level aliases so tests can pin retry sleeps without patching
# time/asyncio globally (same seam as cli/commands/_probe.py's _poll_sleep).
_sleep = time.sleep
_asleep = asyncio.sleep


class Backoff(Protocol):
    """Seconds to wait before retry ``attempt`` (0-based)."""

    def __call__(self, attempt: int) -> float: ...


@dataclass(frozen=True)
class ExponentialBackoff:
    """Bounded exponential backoff: ``base * factor**attempt``, capped.

    The only backoff shape (evaluation-record #14 deleted the fixed-sequence
    variant); a constant schedule is the degenerate ``factor=1.0`` case.
    """

    base: float = 1.0
    factor: float = 2.0
    cap: float = 30.0

    def __call__(self, attempt: int) -> float:
        return min(self.base * (self.factor**attempt), self.cap)


def _agent_phase(span: float) -> float:
    """Deterministic per-process phase offset in [0, span).

    Derived from AVA_AGENT_ID when carried by a launched child so an agent
    keeps its own phase across restarts; falls back to the pid for
    non-agent processes (gateway daemons, CLI); 0 when neither is available
    (tests). Same de-phasing idea as agent/graph/_build.py's
    ``_retry_phase_jitter`` and services/heartbeat/daemon.py's due-time
    jitter: correlated failures hit the whole fleet at once, and an
    identical retry schedule would make every process retry at the same
    instants.
    """
    ident = effective_agent_id()
    if ident is None:
        ident = os.getpid()
    return span * (ident % 1000) / 1000.0


def jittered(delay: float, span: float = 1.0, mode: str = "agent") -> float:
    """``delay`` plus jitter, never negative.

    ``mode``:
    - ``"agent"`` (default) — deterministic per-process phase in [0, span)
      plus random ±span: de-phases a fleet AND spreads within one process.
    - ``"random"`` — random ±span only.
    - ``"none"`` — ``delay`` unchanged (deterministic schedules: probes).

    The result is clamped at 0.0: the jitter's theoretical lower bound is
    ``delay - span``, and a negative sleep raises ValueError from
    ``time.sleep`` — masking the original exception and killing the retry
    path exactly when the retry is the recovery (audit 2026-08-08 P2; the
    `shared/lm/_call.py` retry delay comes from a writable cluster setting,
    so ``delay < span`` is reachable in production, not just a broken
    Policy).
    """
    if mode == "none":
        return delay
    if mode == "random":
        return max(0.0, delay + random.uniform(-span, span))  # noqa: S311 — spread, not secrecy
    return max(0.0, delay + _agent_phase(span) + random.uniform(-span, span))  # noqa: S311


def extract_retry_after(exc: Exception) -> float | None:
    """Seconds the upstream asks us to wait before retrying, or None.

    Duck-typed over any exception shape carrying ``response.headers``
    (provider SDKs, httpx) or a bare ``headers`` attribute (urllib's
    HTTPError). Values above ``_MAX_RETRY_AFTER_RESPECT_S`` are ignored
    (treated as absent) rather than blindly slept.
    """
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "headers"):
        headers = response.headers
    else:
        headers = getattr(exc, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    try:
        raw_ms = headers.get("retry-after-ms")
        if raw_ms:
            seconds = float(raw_ms) / 1000.0
            if 0 < seconds <= _MAX_RETRY_AFTER_RESPECT_S:
                return seconds
    except (TypeError, ValueError):
        pass  # fail-fast-ok: unparseable header = absent; try the next form
    try:
        raw = headers.get("retry-after")
        if raw:
            seconds = float(raw)
            if 0 < seconds <= _MAX_RETRY_AFTER_RESPECT_S:
                return seconds
    except (TypeError, ValueError):
        pass  # fail-fast-ok: unparseable header = absent (SDK parse semantics)
    return None


def _status_of(exc: Exception) -> int | None:
    """HTTP status the exception carries, if it is a status-carrying error."""
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            return int(status)
    return getattr(exc, "code", None)  # urllib.error.HTTPError


def _is_transport(exc: Exception) -> bool:
    """True for connection-level failures (DNS, refused, timeout) — the
    retryable class that carries no HTTP status."""
    if isinstance(exc, urllib.error.URLError):
        return True
    try:
        import httpx
    except ImportError:
        return False
    return isinstance(exc, httpx.TransportError)


class _HttpClassifier:
    """The one HTTP error-classification semantics (design D2).

    Retryable: transient statuses (429/5xx) and transport errors. Permanent:
    any other HTTP status (a deterministic caller/upstream bug) and every
    non-HTTP exception (a deterministic programming error). Call sites
    compose local overrides with ``with_`` instead of re-implementing
    classification (only-override, never-rewrite — grep-able).
    """

    def __init__(
        self,
        *,
        permanent: AbstractSet[int] = frozenset(),
        transient: AbstractSet[int] = frozenset(),
    ) -> None:
        self._permanent = permanent
        self._transient = transient

    def with_(
        self,
        *,
        permanent: AbstractSet[int] = frozenset(),
        transient: AbstractSet[int] = frozenset(),
    ) -> _HttpClassifier:
        """Compose: the same base semantics plus the given status overrides."""
        return _HttpClassifier(
            permanent=self._permanent | permanent,
            transient=self._transient | transient,
        )

    def __call__(self, exc: Exception) -> bool:
        """True when ``exc`` is a retryable failure."""
        status = _status_of(exc)
        if status is not None:
            if status in self._transient:
                return True
            if status in self._permanent:
                return False
            return status in _TRANSIENT_STATUSES
        return _is_transport(exc)


http_classifier = _HttpClassifier()


@dataclass(frozen=True)
class Policy:
    """Immutable retry parameters — the only vocabulary a call site declares.

    - ``max_attempts``: total executions (initial + retries), at least 1.
    - ``backoff``: exponential backoff shape (base / factor / cap).
    - ``jitter`` / ``jitter_span``: sleep spread; see ``jittered``.
    - ``classify``: retryability decision (default ``http_classifier``).
    - ``idempotent``: ``False`` gates retries off entirely — exactly one
      execution, for operations where a blind retry could duplicate an
      effect; the final failure then MUST be visible (``on_final_failure``
      compensation or the raised exception) — silent loss is structurally
      impossible (design D3).
    - ``respect_retry_after``: honor the upstream's Retry-After (capped)
      when it exceeds the backoff.
    - ``on_final_failure``: called with the last exception before it is
      re-raised; may itself raise (e.g. to convert the error type).
    """

    max_attempts: int = 3
    backoff: Backoff = field(default_factory=ExponentialBackoff)
    jitter: str = "agent"
    jitter_span: float = 1.0
    classify: Callable[[Exception], bool] = http_classifier
    idempotent: bool = True
    respect_retry_after: bool = True
    on_final_failure: Callable[[Exception], None] | None = None


def retry(policy: Policy) -> Callable[[Callable[[], T]], T]:
    """Wrap a sync ``fn`` in the one retry loop."""

    def _run(fn: Callable[[], T]) -> T:
        attempts = 1 if not policy.idempotent else max(1, policy.max_attempts)
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as exc:
                if attempt < attempts - 1 and policy.classify(exc):
                    delay = policy.backoff(attempt)
                    if policy.respect_retry_after:
                        retry_after = extract_retry_after(exc)
                        if retry_after is not None:
                            delay = max(delay, retry_after)
                    _sleep(jittered(delay, policy.jitter_span, policy.jitter))
                    continue
                if policy.on_final_failure is not None:
                    policy.on_final_failure(exc)
                raise
        raise AssertionError("unreachable")  # every attempt either returns or raises

    return _run


def aretry(policy: Policy) -> Callable[[Callable[[], Awaitable[T]]], Awaitable[T]]:
    """Wrap an async ``fn`` in the one retry loop (async twin of ``retry``)."""

    def _run(fn: Callable[[], Awaitable[T]]) -> Awaitable[T]:
        async def _runner() -> T:
            attempts = 1 if not policy.idempotent else max(1, policy.max_attempts)
            for attempt in range(attempts):
                try:
                    return await fn()
                except Exception as exc:
                    if attempt < attempts - 1 and policy.classify(exc):
                        delay = policy.backoff(attempt)
                        if policy.respect_retry_after:
                            retry_after = extract_retry_after(exc)
                            if retry_after is not None:
                                delay = max(delay, retry_after)
                        await _asleep(jittered(delay, policy.jitter_span, policy.jitter))
                        continue
                    if policy.on_final_failure is not None:
                        policy.on_final_failure(exc)
                    raise
            raise AssertionError("unreachable")

        return _runner()

    return _run
