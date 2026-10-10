"""Shared process inputs for native takeover notes, independent of graph execution."""

from collections.abc import Callable
from dataclasses import dataclass

from base.clock import Clock

__all__ = ["HandoffNotes"]


@dataclass(frozen=True)
class HandoffNotes:
    """The process owner's live clock and timestamp display policy.

    Construction performs no reads. The agent's note builder evaluates the
    clock factory, wall clock and timestamp reader for each rendered note.
    Controllers can carry these inputs without depending on the graph package.
    """

    clock_factory: Callable[[], Clock]
    timestamps_enabled: Callable[[], bool]
