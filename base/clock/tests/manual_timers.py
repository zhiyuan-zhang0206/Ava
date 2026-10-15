from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

DEFAULT_WALL_START = datetime(2026, 1, 1, tzinfo=UTC)


class ManualTimers:
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
        return self._wall_start + timedelta(seconds=self._now - self._start)

    def advance(self, seconds: float) -> None:
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
