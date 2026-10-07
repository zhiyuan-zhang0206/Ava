"""Serving the understanding tree to the run-timeline — stored nodes with their costs attached.

Pure over stored nodes (`store.load_nodes`): every level of the tree is
served, none dropped or capped; the page draws one row per level, layer 0 (the
message units, `units.py`) below them. Drilling narrows the window; there is no
other cursor.

`level` is the engine level, stable across windows (1 = the finest, leaves; each
level up groups the one below), so a row keeps its number however the window
moves. Each node carries two figures, both computed by code:

- `usage` — the agent's own cost over the node's message span (`usage.MessageUsage`);
- `generation` — the cost of the understanding calls that wrote the node, for the
  nodes that have such a record (leaves written by the chunk consumer); None otherwise.

Storage guarantees at most one cell per same-level region (the write side
reconciles re-cuts away), so nothing here de-duplicates. A node whose time is
unknown (legacy messages) cannot be placed on the time axis and is skipped —
it stays stored, just unserved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from base.agents.history.hierarchy.store import StoredNode
from base.agents.history.hierarchy.usage import GenerationUsage, MessageUsage, Usage


@dataclass(frozen=True)
class ServedNode:
    """One node as the run-timeline serves it."""

    id: str
    level: int
    parent: str | None
    start: datetime
    end: datetime
    span_start: int
    span_end: int
    summary: str
    usage: Usage
    generation: GenerationUsage | None


def serve_nodes(
    nodes: Sequence[StoredNode],
    usage: MessageUsage,
    generation: Mapping[tuple[int, int], GenerationUsage],
    read: Sequence[datetime | None],
) -> list[ServedNode]:
    """The timed nodes, finest level first then in message order, each with its two cost figures.

    A node's `start` / `end` are the read times (`units.read_times`) of the first and last
    message of its span, so a level's nodes never overlap in time and a parent spans exactly its
    children; the stored `start_ts` / `end_ts` are only used to tell a timed node from an untimed one.

    Raises:
        IndexError: a node's span lies outside the history `usage` was built over.
    """
    served: list[ServedNode] = []
    for node in sorted(nodes, key=lambda node: (node.depth, node.span_start)):
        if node.start_ts is None or node.end_ts is None:
            continue
        start, end = read[node.span_start], read[node.span_end]
        if start is None or end is None:
            continue
        served.append(
            ServedNode(
                id=str(node.id),
                level=node.depth,
                parent=str(node.parent_id) if node.parent_id is not None else None,
                start=start,
                end=end,
                span_start=node.span_start,
                span_end=node.span_end,
                summary=node.text,
                usage=usage.span(node.span_start, node.span_end),
                generation=(
                    generation.get((node.span_start, node.span_end)) if node.depth == 1 else None
                ),
            )
        )
    return served
