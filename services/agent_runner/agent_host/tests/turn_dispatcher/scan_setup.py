"""Controllable scheduler and turn progress for hosted wake scan tests."""

from __future__ import annotations

import asyncio

from base.agents.observation.turn_progress import TurnProgress


class ScanScheduler:
    def __init__(
        self,
        active: set[int] | None = None,
        *,
        unwinds_on_cancel: bool = True,
        restart_required: bool = False,
    ) -> None:
        self._active = active or set()
        self._unwinds_on_cancel = unwinds_on_cancel
        self._restart_required = restart_required
        self.woken: list[int] = []
        self.cancelled: list[int] = []
        self.woken_event = asyncio.Event()

    @property
    def active_agents(self) -> frozenset[int]:
        return frozenset(self._active)

    @property
    def restart_required(self) -> bool:
        return self._restart_required

    def wake(self, agent_id: int) -> None:
        self.woken.append(agent_id)
        self.woken_event.set()

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        return None

    def reaped_successor(self, agent_id: int) -> asyncio.Task[None] | None:
        return None

    async def cancel_agent(self, agent_id: int) -> bool:
        self.cancelled.append(agent_id)
        if self._unwinds_on_cancel:
            self._active.discard(agent_id)
        return self._unwinds_on_cancel


class FixedClock(TurnProgress):
    """A turn-progress clock that reports one age for every agent (None: no entry)."""

    def __init__(self, age_s: float | None) -> None:
        super().__init__()
        self._age_s = age_s

    def age_s(self, agent_id: int) -> float | None:
        return self._age_s


__all__ = ["FixedClock", "ScanScheduler"]
