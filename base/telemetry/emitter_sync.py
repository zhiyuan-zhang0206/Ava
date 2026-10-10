"""Per-operation completion receipts for the event pipeline's single writer."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum


class DrainStatus(StrEnum):
    """Canonical completion domain for ordinary event processing."""

    COMPLETED = "completed"
    UNFINISHED = "unfinished"


class DrainPhase(StrEnum):
    """Canonical wait phase of an ordinary event barrier."""

    MARKER = "marker"
    DRAIN = "drain"
    STOP = "stop"


@dataclass(frozen=True)
class DrainResult:
    """Completed barrier or unfinished ordinary projection at its deadline.

    Completed acknowledges writer processing; explicitly isolated sink failures
    are reported independently. Durable audit records and SDK journals own
    separate commit/seal contracts.
    """

    status: DrainStatus
    phase: DrainPhase


@dataclass
class SyncReceipt:
    """One FIFO barrier, acknowledged only after preceding writes succeed."""

    done: threading.Event = field(default_factory=threading.Event)
