"""Serving merge for run-timeline layers — window -> layer node set.

Pure selection over stored nodes (`store.load_window_nodes`): pick the finest
engine level whose intersecting node count fits `max_nodes`, keep every coarser
level as context (the user's "attach each layer"), and convert engine levels to
wire depths relative to the selected top (depth 0 = the top of what the window
shows; the frontend renders one row per depth).

The coverage result drives the response's fallback shape (the three states):
- no usable nodes -> `layers` is None and the raw-context summary carries the
  window;
- full coverage -> layers only;
- partial coverage -> layers plus the raw-context fallback (the agent's
  latest compact summary -- an agent-level text, not sliced to the window);
  the single-summary-field reading of per-segment degradation.

`StoredNode.depth` is the ENGINE level (1 = the finest, leaves); the wire
`depth` is its mirror (`top - level`), so the field name means opposite things
in the two layers — hence the local `level` naming below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from shared.hierarchy.store import StoredNode


@dataclass(frozen=True)
class LayerNode:
    """One wire layer node (storage-agnostic; the router maps it to the schema)."""

    id: str
    depth: int
    parent: str | None
    start: datetime
    end: datetime
    summary: str


@dataclass(frozen=True)
class LayerSelection:
    """The merge outcome: the layer set (None when nothing is usable) and the
    window coverage it provides."""

    layers: tuple[LayerNode, ...] | None
    coverage: str  # none | partial | full


def select_layers(
    nodes: Sequence[StoredNode],
    *,
    window_start: datetime,
    window_end: datetime,
    max_nodes: int,
) -> LayerSelection:
    """Select the layer set for one window; see the module docstring.

    Nodes whose timestamps are unknown (legacy messages) cannot be placed on
    the time axis and are skipped — they stay stored, just unserved.
    """
    usable = [node for node in nodes if node.start_ts is not None and node.end_ts is not None]
    if not usable:
        return LayerSelection(layers=None, coverage="none")
    counts: dict[int, int] = {}
    for node in usable:
        counts[node.depth] = counts.get(node.depth, 0) + 1
    finest: int | None = None
    for level in sorted(counts):
        if counts[level] <= max_nodes:
            finest = level
            break
    if finest is None:  # every level above the cap (cannot happen with a root)
        finest = max(counts)
    selected = [node for node in usable if node.depth >= finest]
    top = max(node.depth for node in selected)
    timed: list[tuple[datetime, datetime, StoredNode]] = [
        (node.start_ts, node.end_ts, node)
        for node in selected
        if node.start_ts is not None and node.end_ts is not None
    ]
    layers: list[LayerNode] = []
    for start, stop, node in sorted(timed, key=lambda trio: (trio[2].depth, trio[0])):
        layers.append(
            LayerNode(
                id=str(node.id),
                depth=top - node.depth,
                parent=str(node.parent_id) if node.parent_id is not None else None,
                start=start,
                end=stop,
                summary=node.text,
            )
        )
    covered = _covers(selected, window_start=window_start, window_end=window_end)
    return LayerSelection(layers=tuple(layers), coverage="full" if covered else "partial")


def _covers(nodes: Sequence[StoredNode], *, window_start: datetime, window_end: datetime) -> bool:
    """Whether the selected nodes' intervals, at any level, cover the window.

    Coverage is a property of the union, not of the coarsest level alone:
    across segments whose trees have different depths, a shallower segment's
    root sits below the selection's global top level, yet its stretch is fully
    explained -- reading only the top level would report a false `partial`
    (harmless in effect, but wrong).
    """
    if not nodes:
        return False
    intervals: list[tuple[datetime, datetime]] = []
    for node in nodes:
        if node.start_ts is not None and node.end_ts is not None:
            intervals.append((node.start_ts, node.end_ts))
    intervals.sort()
    cursor = window_start
    for start, stop in intervals:
        if start > cursor:
            break
        cursor = max(cursor, stop)
        if cursor >= window_end:
            return True
    return False
