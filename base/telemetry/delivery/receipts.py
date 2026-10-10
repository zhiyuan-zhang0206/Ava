"""Per-operation completion receipts for the event pipeline's single writer."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal


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


Category = Literal["audit", "telemetry", "log"]
Level = Literal["debug", "info", "warning", "error", "critical"]


@dataclass(frozen=True)
class Event:
    """One event in the unified stream — OTel LogRecord semantics (events = logs
    with names), the shape the event stream carries."""

    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    cluster: str
    process: str
    category: Category
    event_name: str
    level: Level
    source: str
    target_agent_id: int | None
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])
