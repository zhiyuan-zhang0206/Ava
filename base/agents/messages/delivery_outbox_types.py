"""Value types shared by deferred-delivery outbox callers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlushReport:
    """What one flush pass did; drives tests and the daemon's logging."""

    delivered: int = 0
    buffered: int = 0
    abandoned: int = 0
    deferred: int = 0
    unreadable: int = 0
    expired: int = 0

    @property
    def touched(self) -> int:
        """Whether this pass retired a pending entry."""
        return self.delivered + self.buffered + self.abandoned
