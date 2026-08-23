"""Process-wide fair concurrency budget for gateway-to-Loki reads."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

import httpx

from shared import telemetry

# Must match deploy/lgtm/config/loki.yaml querier.max_concurrent. Every Loki
# read path funnels through gateway.loki_events._get_json and therefore this
# one budget.
LOKI_QUERY_CONCURRENCY = 4
LOKI_QUERY_MAX_WAITERS = 128
LOKI_QUERY_WAIT_TIMEOUT_S = 10.0

BudgetRejectReason = Literal["queue_full", "acquire_timeout"]
BudgetOutcome = Literal["queued", "acquired", "released", "queue_full", "wait_timeout", "cancelled"]
_log = logging.getLogger(__name__)


class LokiQueryBudgetError(httpx.PoolTimeout):
    """A local Loki-capacity refusal with a stable machine-readable reason."""

    def __init__(self, reason: BudgetRejectReason) -> None:
        message = (
            "Loki query queue is full"
            if reason == "queue_full"
            else "timed out waiting for the Loki query budget"
        )
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class BudgetMetrics:
    """Atomic state/counter snapshot for tests and local diagnostics."""

    active: int
    queued: int
    high_water: int
    acquired: int
    queue_full: int
    wait_timeout: int


@dataclass(frozen=True)
class BudgetObservation:
    """One state transition, shaped for the unified telemetry registry.

    active/queued/high_water are float observations, so the automatic OTLP
    mapping records concurrency distributions. The three integer outcome
    fields are 0/1 deltas and therefore remain correct Prometheus counters.
    """

    outcome: BudgetOutcome
    active: float
    queued: float
    high_water: float
    wait_ms: float
    acquired: int
    queue_full: int
    wait_timeout: int


BudgetObserver = Callable[[BudgetObservation], None]
BudgetErrorFactory = Callable[[BudgetRejectReason], httpx.PoolTimeout]


def _emit_observation(observation: BudgetObservation) -> None:
    """Enqueue one observation without DB or Loki I/O on this caller."""
    telemetry.emit(
        "telemetry",
        "loki_query_budget",
        attributes={
            "outcome": observation.outcome,
            "active": observation.active,
            "queued": observation.queued,
            "high_water": observation.high_water,
            "wait_ms": observation.wait_ms,
            "acquired": observation.acquired,
            "queue_full": observation.queue_full,
            "wait_timeout": observation.wait_timeout,
        },
    )


class FairQueryBudget:
    """FIFO slots with bounded queue length, wait time, and transition metrics."""

    def __init__(
        self,
        *,
        capacity: int,
        max_waiters: int,
        wait_timeout_s: float,
        observer: BudgetObserver | None = None,
        error_factory: BudgetErrorFactory = LokiQueryBudgetError,
    ) -> None:
        self._capacity = capacity
        self._max_waiters = max_waiters
        self._wait_timeout_s = wait_timeout_s
        self._observer = observer
        self._error_factory = error_factory
        self._condition = threading.Condition()
        self._active = 0
        self._queue: deque[object] = deque()
        self._high_water = 0
        self._acquired = 0
        self._queue_full = 0
        self._wait_timeout = 0

    def _observation(
        self,
        outcome: BudgetOutcome,
        *,
        wait_ms: float = 0.0,
        acquired: int = 0,
        queue_full: int = 0,
        wait_timeout: int = 0,
    ) -> BudgetObservation:
        """Capture one transition while `_condition` is held."""
        return BudgetObservation(
            outcome=outcome,
            active=float(self._active),
            queued=float(len(self._queue)),
            high_water=float(self._high_water),
            wait_ms=wait_ms,
            acquired=acquired,
            queue_full=queue_full,
            wait_timeout=wait_timeout,
        )

    def _observe(self, observation: BudgetObservation) -> None:
        """Call the observer outside the state lock; it cannot deadlock transitions."""
        if self._observer is None:
            return
        try:
            self._observer(observation)
        except Exception:
            _log.exception("query budget observer failed")

    def _acquire(self) -> None:
        queued_at = time.monotonic()
        ticket = object()
        rejection: httpx.PoolTimeout | None = None
        with self._condition:
            if self._active < self._capacity and not self._queue:
                self._active += 1
                self._acquired += 1
                observation = self._observation("acquired", acquired=1)
                acquired_immediately = True
            elif len(self._queue) >= self._max_waiters:
                self._queue_full += 1
                observation = self._observation("queue_full", queue_full=1)
                rejection = self._error_factory("queue_full")
                acquired_immediately = False
            else:
                self._queue.append(ticket)
                self._high_water = max(self._high_water, len(self._queue))
                observation = self._observation("queued")
                acquired_immediately = False
        self._observe(observation)
        if rejection is not None:
            raise rejection
        if acquired_immediately:
            return
        self._wait_for_slot(ticket, queued_at)

    def _wait_for_slot(self, ticket: object, queued_at: float) -> None:
        """Wait for an already-enqueued ticket, removing it on every exit path."""
        deadline = queued_at + self._wait_timeout_s
        timeout_error: httpx.PoolTimeout | None = None
        observation: BudgetObservation | None = None
        try:
            with self._condition:
                while self._queue[0] is not ticket or self._active >= self._capacity:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._queue.remove(ticket)
                        self._wait_timeout += 1
                        self._condition.notify_all()
                        observation = self._observation(
                            "wait_timeout",
                            wait_ms=(time.monotonic() - queued_at) * 1000.0,
                            wait_timeout=1,
                        )
                        timeout_error = self._error_factory("acquire_timeout")
                        break
                    self._condition.wait(timeout=remaining)
                if timeout_error is None:
                    self._queue.popleft()
                    self._active += 1
                    self._acquired += 1
                    observation = self._observation(
                        "acquired",
                        wait_ms=(time.monotonic() - queued_at) * 1000.0,
                        acquired=1,
                    )
        except BaseException:
            with self._condition:
                with suppress(ValueError):
                    self._queue.remove(ticket)
                self._condition.notify_all()
                observation = self._observation(
                    "cancelled", wait_ms=(time.monotonic() - queued_at) * 1000.0
                )
            self._observe(observation)
            raise
        if observation is None:
            raise RuntimeError("Loki query budget wait ended without a state transition")
        self._observe(observation)
        if timeout_error is not None:
            raise timeout_error

    def _release(self) -> None:
        with self._condition:
            self._active -= 1
            observation = self._observation("released")
            self._condition.notify_all()
        self._observe(observation)

    @contextmanager
    def slot(self) -> Generator[None, None, None]:
        self._acquire()
        try:
            yield
        finally:
            self._release()

    def metrics(self) -> BudgetMetrics:
        """Return a lock-consistent snapshot without calling any observer."""
        with self._condition:
            return BudgetMetrics(
                active=self._active,
                queued=len(self._queue),
                high_water=self._high_water,
                acquired=self._acquired,
                queue_full=self._queue_full,
                wait_timeout=self._wait_timeout,
            )


query_budget = FairQueryBudget(
    capacity=LOKI_QUERY_CONCURRENCY,
    max_waiters=LOKI_QUERY_MAX_WAITERS,
    wait_timeout_s=LOKI_QUERY_WAIT_TIMEOUT_S,
    observer=_emit_observation,
)


def reset_for_tests(
    *,
    capacity: int = LOKI_QUERY_CONCURRENCY,
    max_waiters: int = LOKI_QUERY_MAX_WAITERS,
    wait_timeout_s: float = LOKI_QUERY_WAIT_TIMEOUT_S,
    observer: BudgetObserver | None = None,
    error_factory: BudgetErrorFactory = LokiQueryBudgetError,
) -> None:
    """Replace the process budget between isolated unit tests."""
    global query_budget  # noqa: PLW0603 — intentional process singleton test seam
    query_budget = FairQueryBudget(
        capacity=capacity,
        max_waiters=max_waiters,
        wait_timeout_s=wait_timeout_s,
        observer=observer,
        error_factory=error_factory,
    )
