# ava:ref services/derived/insights/run_timeline/docs/range-projection.ava.okf.md#indexed-history-windows
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from base.agents.history.checkpoint import (
    FullHistory,
)
from base.agents.history.hierarchy.units import (
    DisplayBlock,
)
from base.agents.history.hierarchy.usage import MessageUsage
from base.agents.history.message_tokens import (
    MessageTokens,
    SegmentTokens,
    history_message_tokens,
    history_segment_tokens,
)


@dataclass(frozen=True)
class HistoryView:
    history: FullHistory
    units: list[DisplayBlock]
    usage: MessageUsage
    read: list[datetime | None]
    segments: tuple[SegmentTokens, ...]
    tokens: list[MessageTokens]

    @classmethod
    def of(
        cls,
        history: FullHistory,
        units: list[DisplayBlock],
        usage: MessageUsage,
        read: list[datetime | None],
    ) -> HistoryView:
        segments = history_segment_tokens(history)
        tokens = history_message_tokens(history, segments)
        return cls(history, units, usage, read, segments, tokens)

    @property
    def extent(self) -> tuple[datetime, datetime] | None:
        if not self.units:
            return None
        return min(unit.start for unit in self.units), max(unit.end for unit in self.units)
