"""Live rendering policy owned by one history producer."""

from collections.abc import Callable
from dataclasses import dataclass

from base.clock import Clock

__all__ = ["TimelineReadInputs"]


@dataclass(frozen=True)
class TimelineReadInputs:
    """Live rendering policy supplied by the history producer. Construction reads nothing."""

    clock_factory: Callable[[], Clock]
    timestamps_enabled: Callable[[], bool]
