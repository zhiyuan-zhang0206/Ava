"""Machine-wide screen ownership for the computer-use daemon (Phase 2).

The desktop is one shared screen that multiple agents drive through the same
daemon. Phase 1 serialized individual actions; this module adds session-level
coordination so one agent's multi-step flow (snapshot -> think -> click -> ...)
is not interleaved by another agent's actions:

- **holder**: the agent that currently owns the screen. The first action from
  an idle screen implicitly acquires ownership — no separate acquire tool
  (Keep It Simple, per the Phase 2 design review).
- **lease**: every action renews the holder's lease (default 30s). A holder
  that stops acting for the lease duration loses ownership automatically — a
  hung session cannot pin the screen forever.
- **FIFO queue**: while someone else holds the screen, a request waits up to
  the queue timeout (default 30s) for its turn; on timeout it fails with a
  readable "screen busy" error instead of interleaving.
- **release**: explicit handover by the holder (`release_control` tool) or an
  operator kick (CLI, no identity) — the next waiter takes over.

This is resource coordination, not governance: nothing is refused based on
who the agent is (peer trust model, user ruling 2026-08-10). The daemon's
action lock still serializes individual executions; ownership decides WHO
may act, the lock decides WHEN.

Config via env (daemon is a per-machine service):
  AVA_COMPUTER_LEASE_S          (default 30)
  AVA_COMPUTER_QUEUE_TIMEOUT_S  (default 30)
"""

from __future__ import annotations

import asyncio
from collections import deque

# Waiter poll interval: short enough that a released screen is re-taken
# promptly, long enough that idle waiters cost nothing.
_POLL_S = 0.05

DEFAULT_LEASE_S = 30.0
DEFAULT_QUEUE_TIMEOUT_S = 30.0


class ScreenSession:
    """One holder + a FIFO of waiters, protected by a single state lock."""

    def __init__(
        self, lease_s: float = DEFAULT_LEASE_S, queue_timeout_s: float = DEFAULT_QUEUE_TIMEOUT_S
    ) -> None:
        self._lease_s = lease_s
        self._queue_timeout_s = queue_timeout_s
        self._holder: int | None = None
        self._lease_until: float = 0.0  # loop.time() (monotonic)
        self._waiters: deque[tuple[asyncio.Future[bool], str]] = deque()
        self._state_lock = asyncio.Lock()

    @property
    def holder(self) -> int | None:
        """Current holder, for diagnostics (no lock — best effort)."""
        return self._holder

    def _take(self, agent_id: int, now: float) -> None:
        """Become the holder (caller holds the state lock)."""
        self._holder = agent_id
        self._lease_until = now + self._lease_s

    async def acquire(self, agent_id: int, priority: str = "normal") -> bool:
        """Become the holder (or confirm we are), waiting FIFO up to the queue
        timeout. `priority="high"` jumps the queue (FIFO among highs — a P0
        task is not blocked behind a normal task's whole session). Returns
        False on timeout — the caller should fail the request with a busy
        error rather than interleaving on a held screen."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        queued = False
        deadline = loop.time() + self._queue_timeout_s
        try:
            while True:
                async with self._state_lock:
                    now = loop.time()
                    free = self._holder is None or now >= self._lease_until
                    if free:
                        if self._waiters and self._waiters[0][0] is not fut:
                            # The screen is free but someone queued before us —
                            # join the tail and let them take it first.
                            if not queued:
                                self._enqueue(fut, priority)
                                queued = True
                        else:
                            if self._waiters:
                                self._waiters.popleft()
                            self._take(agent_id, now)
                            return True
                    elif self._holder == agent_id:
                        # Already the holder — pass through without re-queueing.
                        return True
                    else:
                        if not queued:
                            self._enqueue(fut, priority)
                            queued = True
                        if now >= deadline:
                            self._dequeue(fut)
                            return False
                await asyncio.sleep(_POLL_S)
        except asyncio.CancelledError:
            # A cancelled waiter must not keep occupying its queue slot.
            if queued:
                async with self._state_lock:
                    self._dequeue(fut)
            raise

    def _enqueue(self, fut: asyncio.Future[bool], priority: str) -> None:
        """Join the waiters (caller holds the state lock): high priority
        inserts after the existing highs (FIFO among highs), normal appends."""
        if priority != "high":
            self._waiters.append((fut, "normal"))
            return
        insert_at = 0
        for i, (_f, p) in enumerate(self._waiters):
            if p != "high":
                break
            insert_at = i + 1
        self._waiters.insert(insert_at, (fut, "high"))

    def _dequeue(self, fut: asyncio.Future[bool]) -> None:
        """Remove a specific waiter (caller holds the state lock)."""
        for i, (f, _p) in enumerate(self._waiters):
            if f is fut:
                del self._waiters[i]
                return

    async def touch(self, agent_id: int) -> None:
        """Renew the holder's lease (a no-op for non-holders)."""
        async with self._state_lock:
            if self._holder == agent_id:
                self._lease_until = asyncio.get_running_loop().time() + self._lease_s

    async def release(self, agent_id: int | None = None) -> int | None:
        """Release the screen. agent_id None = operator kick (any holder).
        Returns the released holder, or None when nobody was released."""
        async with self._state_lock:
            if self._holder is not None and (agent_id is None or self._holder == agent_id):
                released = self._holder
                self._holder = None
                self._lease_until = 0.0
                return released
            return None

    def __repr__(self) -> str:
        return (
            f"ScreenSession(holder={self._holder}, "
            f"lease_until={self._lease_until:.1f}, waiters={len(self._waiters)})"
        )
