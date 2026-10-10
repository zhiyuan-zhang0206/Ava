"""One agent's stitched message history, read once and cached shortly, with what the page derives from it.

Every run-timeline read (the window, the raw-message ranges) needs the same
thing: the full history across compaction segments, its layer-0 units
(`hierarchy.units`) and its usage prefix sums (`hierarchy.usage`). Reading and
deriving them costs a checkpoint reconstruction, and drilling fires one request
per click, so the derived view is kept per agent and re-validated against the newest
checkpoint id. A reader that
finds the view behind the understanding tree (a node written after the view was
built) says how many messages it needs, and a view with fewer is rebuilt.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

from base.agents.history.checkpoint import (
    FullHistory,
    latest_checkpoint_id,
    load_checkpoint_history_full,
)
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
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.db import Database

# Short enough that a live agent's growth shows within a click or two; the cache
# only has to absorb the burst of requests one page interaction makes.
_TTL_SECONDS = 5.0
# A view is rebuilt for a node it does not reach at most this often.
_REFRESH_SECONDS = 2.0
# A view whose checkpoint id is unchanged is kept this long at most, however often it is re-validated.
_MAX_AGE_SECONDS = 600.0
# One agent's view is its whole history, about 12 KB per message measured on the preview
# (a 4.6k-message agent retains ~49 MB, a 200-message worker ~3 MB). The agent view puts
# several agents on one screen, so one page load needs all of them cached at once.
_MAX_ENTRIES = 16


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


@dataclass(frozen=True)
class _Entry:
    """A cached view, when it was last confirmed current, and the checkpoint id it was read at."""

    at: float
    head: str | None
    view: HistoryView


class HistoryViewCache:
    """A few agents' views; one instance lives on the app (`app.state.run_timeline_views`).

    Key: the agent id. A view is served untouched for `_TTL_SECONDS`; after that one cheap
    read of the agent's newest checkpoint id says whether the history moved: an unchanged id
    keeps the view (up to `_MAX_AGE_SECONDS`), a new one rebuilds it. Builds are single-flight
    per agent, so the burst of requests one page load makes costs one checkpoint read, not one each.
    """

    def __init__(self, *, timeline_inputs: TimelineReadInputs) -> None:
        self._timeline_inputs = timeline_inputs
        self._lock = threading.Lock()
        self._entries: dict[int, _Entry] = {}
        self._building: dict[int, threading.Lock] = {}

    def _fresh(self, hit: _Entry | None, now: float, needs: int) -> HistoryView | None:
        if hit is None or now - hit.at > _TTL_SECONDS:
            return None
        behind = len(hit.view.history.messages) < needs
        return hit.view if not behind or now - hit.at < _REFRESH_SECONDS else None

    def get(self, db: Database, agent_id: int, *, needs: int = 0) -> HistoryView:
        """The agent's view. `needs` is how many messages the caller must see (a node written
        after the cached view was built reaches past it): a view with fewer is rebuilt, but not more
        than once per `_REFRESH_SECONDS`, so a node that reaches past the history for good (an
        orphan) cannot make every request pay for a full rebuild."""
        with self._lock:
            view = self._fresh(self._entries.get(agent_id), time.monotonic(), needs)
            if view is not None:
                return view
            build = self._building.setdefault(agent_id, threading.Lock())
        with build:  # concurrent readers of one agent wait for the first build and share it
            now = time.monotonic()
            with self._lock:
                hit = self._entries.get(agent_id)
            view = self._fresh(hit, now, needs)
            if view is not None:
                return view
            # Read before the history: a commit in between leaves the id older than the view,
            # so the next check rebuilds rather than keeps a stale view.
            head = latest_checkpoint_id(db, agent_id)
            if (
                hit is not None
                and head == hit.head
                and len(hit.view.history.messages) >= needs
                and now - hit.at <= _MAX_AGE_SECONDS
            ):
                with self._lock:
                    self._entries[agent_id] = _Entry(now, head, hit.view)
                return hit.view
            history = load_checkpoint_history_full(db, agent_id)
            read = read_times(history.messages, timeline_inputs=self._timeline_inputs)
            units = display_blocks(
                divide_units(history.messages, timeline_inputs=self._timeline_inputs),
                history.messages,
                read,
            )
            view = HistoryView.of(history, units, MessageUsage(history.messages), read)
            with self._lock:
                self._entries.pop(agent_id, None)
                self._entries[agent_id] = _Entry(time.monotonic(), head, view)
                while len(self._entries) > _MAX_ENTRIES:
                    evicted = next(iter(self._entries))
                    self._entries.pop(evicted)
                    if evicted != agent_id:
                        self._building.pop(evicted, None)
            return view
