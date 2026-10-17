from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


def recovery_permitted() -> bool:
    from base.deploy.lifecycle import start_serving

    return start_serving.is_serving()


def _now() -> float:
    return time.monotonic()


@dataclass
class RecoveryGate:
    alert_after_s: float
    emit_held: Callable[[float], None]
    emit_refusal: Callable[[int, str], None]
    permitted: Callable[[], bool] = recovery_permitted
    now: Callable[[], float] = _now
    held_since: float | None = None
    alerted: bool = False
    refused: set[int] = field(default_factory=set[int])

    def __post_init__(self) -> None:
        if not self.alert_after_s > 0:
            raise ValueError("alert_after_s must be positive")

    def observe(self) -> None:
        if self.permitted():
            self._lift()
            return
        now = self.now()
        if self.held_since is None:
            self.held_since = now
            self.alerted = False
            self.refused.clear()
            return
        if not self.alerted and now - self.held_since >= self.alert_after_s:
            self.alerted = True
            self.emit_held(now - self.held_since)

    def refusal(self, schedule_id: int, reason: str) -> None:
        if schedule_id in self.refused:
            return
        self.refused.add(schedule_id)
        self.emit_refusal(schedule_id, reason)

    def _lift(self) -> None:
        self.held_since = None
        self.alerted = False
        self.refused.clear()
