from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from base.sessions.helper_chain_guard import helper_marker_pid, parent_chain_intact


@dataclass(frozen=True)
class HelperChainConfig:
    interval_s: float = 5.0
    confirm_rounds: int = 2

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_s) or self.interval_s <= 0:
            raise ValueError("interval_s must be finite and positive")
        if self.confirm_rounds < 1:
            raise ValueError("confirm_rounds must be at least 1")


class HelperChainWatch:
    def __init__(
        self,
        *,
        retire: Callable[[str], None],
        stop: asyncio.Event,
        config: HelperChainConfig | None = None,
        intact: Callable[[], bool] = parent_chain_intact,
        marker: Callable[[], str | None] = helper_marker_pid,
    ) -> None:
        self._retire = retire
        self._stop = stop
        self._config = config if config is not None else HelperChainConfig()
        self._intact = intact
        self._marker = marker

    async def run(self) -> None:
        broken_rounds = 0
        # quiesce-exempt: meiva:ref services/supervision/meiva_root/docs/ancestry.meiva.okf.md#helper-ancestry-watch
        while not self._stop.is_set():
            if self._intact():
                broken_rounds = 0
            else:
                broken_rounds += 1
                if broken_rounds >= self._config.confirm_rounds:
                    self._retire(self._detail(broken_rounds))
                    return
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._config.interval_s)

    def _detail(self, broken_rounds: int) -> str:
        return (
            f"root pid {os.getpid()} is no longer under the helper that seeded it "
            f"(marker pid {self._marker() or 'absent'}, "
            f"{broken_rounds} consecutive broken readings)"
        )
