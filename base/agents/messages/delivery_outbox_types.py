"""Value types shared by deferred-delivery outbox callers."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol


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


class FlushPool(Protocol):
    """Structural type of the ops daemon's connection pool.

    Kept psycopg-free so the sender-side import of this module stays light:
    only the flush side (the ops daemon) holds a real pool, and it passes it
    in. `connection` mirrors the psycopg_pool call the flush makes — a bounded
    wait so a down data plane cannot park the flusher past its next tick.
    """

    def connection(self, *, timeout: float | None = None) -> AbstractContextManager[Any]: ...


class PermanentDeliveryError(Exception):
    """The record can never be delivered; abandon it with this reason.

    `detail` carries the readable upstream text (the exception that decided the
    refusal, when one exists), so the abandonment record explains its code
    instead of only naming it.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
