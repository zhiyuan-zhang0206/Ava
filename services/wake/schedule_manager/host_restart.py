from __future__ import annotations

import logging
import time

from base import telemetry

_log = logging.getLogger(__name__)


# meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
class AutoRestartLedger:
    def __init__(self) -> None:
        self._attempts: dict[int, list[float]] = {}
        self._intents: dict[int, tuple[str, str]] = {}

    def recent(self, schedule_id: int, window_seconds: float) -> list[float]:
        now = time.monotonic()
        return [seen for seen in self._attempts.get(schedule_id, []) if now - seen < window_seconds]

    def in_flight(self, schedule_id: int) -> tuple[str, str] | None:
        return self._intents.get(schedule_id)

    def record(self, schedule_id: int, intent: tuple[str, str], window_seconds: float) -> int:
        attempts = self.recent(schedule_id, window_seconds)
        attempts.append(time.monotonic())
        self._attempts[schedule_id] = attempts
        self._intents[schedule_id] = intent
        return len(attempts)

    def clear(self, schedule_id: int) -> tuple[str, str] | None:
        return self._intents.pop(schedule_id, None)

    def prune(self, known: set[int]) -> None:
        self._attempts = {sid: seen for sid, seen in self._attempts.items() if sid in known}
        self._intents = {sid: intent for sid, intent in self._intents.items() if sid in known}


# meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
def emit_auto_restart(
    schedule_id: int, previous_generation: str, generation: str, restarts_in_window: int
) -> None:
    telemetry.emit(
        "telemetry",
        event_name="schedule_auto_restart",
        level="warning",
        attributes={
            "schedule_id": schedule_id,
            "previous_generation": previous_generation,
            "current_generation": generation,
            "restarts_in_window": restarts_in_window,
        },
    )


# meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
def emit_auto_restart_suppressed(
    schedule_id: int,
    previous_generation: str,
    generation: str,
    restarts_in_window: int,
    window_seconds: float,
) -> None:
    _log.error(
        "schedule %s: %d automatic relaunches in %ss; giving up until an explicit restart",
        schedule_id,
        restarts_in_window,
        window_seconds,
    )
    telemetry.emit(
        "telemetry",
        event_name="schedule_auto_restart_suppressed",
        level="warning",
        attributes={
            "schedule_id": schedule_id,
            "previous_generation": previous_generation,
            "current_generation": generation,
            "restarts_in_window": restarts_in_window,
        },
    )
