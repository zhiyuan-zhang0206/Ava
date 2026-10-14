"""`ManualTimers`: a `Timers` for tests that moves virtual time and never waits.

Pass it wherever production code takes a `Timers`. `monotonic()` reads the virtual clock,
`advance(seconds)` moves it, and `sleep` / `asleep` move it by the requested amount and record
the request in `sleeps`, so a test asserts on what the code waited for ([1, 2, 4] for a
three-step backoff) instead of waiting for it. `now()` is a wall clock that moves with it, for a
component that also takes a `Clock`: `Clock(config, now=timers.now)` shares the one virtual time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

# A fixed, timezone-aware starting instant, so a test reads the same wall time on every run.
DEFAULT_WALL_START = datetime(2026, 1, 1, tzinfo=UTC)


class ManualTimers:
    """Virtual elapsed time; nothing waits.

    `sleep` follows `time.sleep` and rejects a negative duration; `asleep` follows
    `asyncio.sleep` and treats a non-positive one as no wait. Either way `sleeps` keeps the
    requested value, and the virtual clock moves by the non-negative part of it. `asleep` still
    yields to the event loop once, as `asyncio.sleep` does, so a loop that only ever awaits
    `asleep` lets other tasks run.
    """

    def __init__(self, start: float = 0.0, *, wall_start: datetime = DEFAULT_WALL_START) -> None:
        if wall_start.tzinfo is None:
            raise ValueError("wall_start must be timezone-aware")
        self._start = start
        self._now = start
        self._wall_start = wall_start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def now(self) -> datetime:
        """The wall clock at the virtual time: `wall_start` plus everything elapsed so far."""
        return self._wall_start + timedelta(seconds=self._now - self._start)

    def advance(self, seconds: float) -> None:
        """Move the virtual clock forward by `seconds` without recording a sleep."""
        if seconds < 0:
            raise ValueError(f"time cannot move backwards: advance({seconds})")
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        self.sleeps.append(seconds)
        self._now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += max(seconds, 0.0)
        await asyncio.sleep(0)
