"""Fair admission gate for hosted turns.

The gate bounds how many turns execute concurrently on one host; excess turns
queue and are served in arrival order. It deliberately does not reimplement
scheduling: ``TurnScheduler`` (``services/agent_host/dispatcher.py``) already
guarantees at most one turn task per agent, so each agent holds at most one
ticket, and a completed turn's next request is a new ticket at the tail.
Together that is ticket rotation — arrival-order service, per-agent single
flight, no starvation across agents.

The queue primitive stays ``asyncio.Semaphore``, whose waiters wake in FIFO
order (CPython 3.12 wakes the oldest live waiter; locked by the wake-order test
in tests/services/test_turn_admission.py). This class adds what the bare
semaphore cannot express:

- **The waiting state.** A waiter is registered in ``agent/_turn_progress.py``
  BEFORE the acquire await, so there is no window in which a parked turn is
  invisible to the dispatcher's fake-alive scan. Cancelling a waiter would only
  re-queue it at the tail (its successor is a new ticket), so the wait is
  exempted explicitly rather than left to the stall budget.
- **Observability.** Queue depth, current waiter ages and served-wait counters
  feed the daemon's ``/stats``; the beat loop reports each waiter that crosses
  the alert bound once per episode as the ``host_admission_wait_exceeded``
  anomaly event. Metrics are read-only; the event is a signal, never a
  cancellation.

``limit <= 0`` disables the gate: ``admit()`` yields without touching any
state, preserving the documented zero-limit semantics.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from agent._turn_progress import begin_admission_wait, end_admission_wait


class TurnAdmission:
    """Slot gate plus wait bookkeeping for one host process's hosted turns."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._slots = asyncio.Semaphore(limit) if limit > 0 else None
        # agent_id -> wait start (time.monotonic()) while parked at the gate.
        self._waiting: dict[int, float] = {}
        # Agents already reported for their CURRENT wait episode (alert dedup).
        self._alerted: set[int] = set()
        self.waits_total = 0
        self.wait_seconds_total = 0.0
        self.wait_seconds_max = 0.0

    @property
    def enabled(self) -> bool:
        return self._slots is not None

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    @asynccontextmanager
    async def admit(self, agent_id: int) -> AsyncGenerator[None, None]:
        """Hold one slot for this turn, queuing first-in-first-out when full.

        Registration precedes the acquire await by construction: the ``queued``
        check and ``begin_admission_wait`` run with no await between them and
        the semaphore's own fast path is synchronous, so a registered waiter is
        exactly a turn that had to queue.
        """
        if self._slots is None:
            yield
            return
        started = time.monotonic()
        queued = self._slots.locked()
        if queued:
            self._waiting[agent_id] = started
            begin_admission_wait(agent_id)
        try:
            await self._slots.acquire()
        except BaseException:
            if queued:
                self._leave_queue(agent_id)
            raise
        if queued:
            self._leave_queue(agent_id)
            waited = time.monotonic() - started
            self.waits_total += 1
            self.wait_seconds_total += waited
            self.wait_seconds_max = max(self.wait_seconds_max, waited)
        try:
            yield
        finally:
            self._slots.release()

    def _leave_queue(self, agent_id: int) -> None:
        self._waiting.pop(agent_id, None)
        self._alerted.discard(agent_id)
        end_admission_wait(agent_id)

    def waiting_agents(self) -> dict[int, float]:
        """Current waiters and how long each has queued, in seconds."""
        now = time.monotonic()
        return {agent_id: now - started for agent_id, started in self._waiting.items()}

    def long_waiters(self, threshold_s: float) -> list[tuple[int, float]]:
        """Waiters at or past ``threshold_s``, each reported once per episode.

        Called from the daemon's beat loop; a still-queued waiter is marked, so
        it does not re-fire on the next beat, and the mark clears when the wait
        ends (served or cancelled) so its NEXT wait can alert again.
        """
        now = time.monotonic()
        reported: list[tuple[int, float]] = []
        for agent_id, started in self._waiting.items():
            waited = now - started
            if waited >= threshold_s and agent_id not in self._alerted:
                self._alerted.add(agent_id)
                reported.append((agent_id, waited))
        return reported

    def payload(self) -> dict[str, object]:
        """The ``/stats`` admission block."""
        now = time.monotonic()
        return {
            "admission_limit": self.limit,
            "admission_waiting": len(self._waiting),
            "admission_waiting_agents": {
                str(agent_id): round(now - started, 1)
                for agent_id, started in sorted(self._waiting.items())
            },
            "admission_waits_total": self.waits_total,
            "admission_wait_seconds_total": round(self.wait_seconds_total, 1),
            "admission_wait_seconds_max": round(self.wait_seconds_max, 1),
        }
