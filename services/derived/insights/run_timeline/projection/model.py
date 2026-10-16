# ava:ref services/derived/insights/run_timeline/docs/range-projection.ava.okf.md#indexed-history-windows
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.schemas import RunTimelineUnit

VERSION = 1


class Totals(BaseModel):
    model_config = ConfigDict(frozen=True)
    calls: int = 0
    input: int = 0
    cache_read: int = 0
    output: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    cost_calls: int = 0
    context: int = 0
    known: int = 0
    estimated: int = 0


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True)
    idx: int
    start_ts: datetime | None
    end_ts: datetime | None
    request_ts: datetime | None
    before_totals: Totals
    after_totals: Totals


class Segment(BaseModel):
    model_config = ConfigDict(frozen=True)
    session: int
    checkpoint_id: str
    message_start: int
    message_end: int
    local_start: int
    read_before: datetime | None
    read_after: datetime | None
    start_ts: datetime | None
    end_ts: datetime | None
    totals: Totals

    @property
    def offset(self) -> int:
        return self.message_start - self.local_start


@dataclass(frozen=True)
class Projection:
    segment: Segment
    facts: list[Fact]
    blocks: list[RunTimelineUnit]
    view: HistoryView
