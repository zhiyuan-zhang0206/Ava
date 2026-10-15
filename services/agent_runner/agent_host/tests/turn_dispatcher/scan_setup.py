from __future__ import annotations

import asyncio


class ScanScheduler:
    def __init__(
        self,
        active: set[int] | None = None,
        *,
        restart_required: bool = False,
    ) -> None:
        self._active = active or set()
        self._restart_required = restart_required
        self.woken: list[int] = []
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


__all__ = ["ScanScheduler"]
