"""One agent's stitched message history, read once and cached shortly, with what the page derives from it.

Every run-timeline read (the window, the raw-message ranges) needs the same
thing: the full history across compaction segments, its layer-0 units
(`hierarchy.units`) and its usage prefix sums (`hierarchy.usage`). Reading and
deriving them costs a checkpoint reconstruction, and drilling fires one request
per click, so the derived view is kept for a few seconds per agent. A reader that
finds the view behind the understanding tree (a node written after the view was
built) says how many messages it needs, and a view with fewer is rebuilt.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

from base.agents.history.checkpoint import FullHistory, load_checkpoint_history_full
from base.agents.history.hierarchy.units import (
    DisplayBlock,
    display_blocks,
    divide_units,
    read_times,
)
from base.agents.history.hierarchy.usage import MessageUsage
from base.agents.history.message_tokens import (
    MessageTokens,
    SegmentTokens,
    history_message_tokens,
    history_segment_tokens,
)
from base.db import Database

# Short enough that a live agent's growth shows within a click or two; the cache
# only has to absorb the burst of requests one page interaction makes.
_TTL_SECONDS = 5.0
# A view is rebuilt for a node it does not reach at most this often.
_REFRESH_SECONDS = 2.0
# One agent's view is its whole history; the gateway is shared by concurrently
# viewed agents, so only a few are kept.
_MAX_ENTRIES = 6


@dataclass(frozen=True)
class HistoryView:
    """The stitched history with its timeline blocks (units, a work unit split in three; on read times) and usage sums.

    `read` is the time each message was read by the model (`units.read_times`); units and nodes
    are placed on it, so neither overlaps its neighbours on the time axis. `segments` are the
    per-segment token counts (`message_tokens`) and `tokens` the same, one per message of the
    stitched history.
    """

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
        """The view of `history`, its per-message token counts derived with it."""
        segments = history_segment_tokens(history)
        tokens = history_message_tokens(history, segments)
        return cls(history, units, usage, read, segments, tokens)

    @property
    def extent(self) -> tuple[datetime, datetime] | None:
        """The first and last placeable time among the units, or None when none has one."""
        if not self.units:
            return None
        return min(unit.start for unit in self.units), max(unit.end for unit in self.units)


class HistoryViewCache:
    """A few agents' views for a few seconds; one instance lives on the app (`app.state.run_timeline_views`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, tuple[float, HistoryView]] = {}

    def get(self, db: Database, agent_id: int, *, needs: int = 0) -> HistoryView:
        """The agent's view. `needs` is how many messages the caller must see (a node written
        after the cached view was built reaches past it): a view with fewer is rebuilt, but not more
        than once per `_REFRESH_SECONDS`, so a node that reaches past the history for good (an
        orphan) cannot make every request pay for a full rebuild."""
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(agent_id)
            if hit is not None and now - hit[0] <= _TTL_SECONDS:
                behind = len(hit[1].history.messages) < needs
                if not behind or now - hit[0] < _REFRESH_SECONDS:
                    return hit[1]
        history = load_checkpoint_history_full(db, agent_id)
        read = read_times(history.messages)
        units = display_blocks(divide_units(history.messages), history.messages, read)
        view = HistoryView.of(history, units, MessageUsage(history.messages), read)
        with self._lock:
            self._entries.pop(agent_id, None)
            self._entries[agent_id] = (now, view)
            while len(self._entries) > _MAX_ENTRIES:
                self._entries.pop(next(iter(self._entries)))
        return view
