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
from base.agents.history.hierarchy.units import DisplayBlock
from base.agents.history.hierarchy.usage import GenerationUsage, MessageUsage, Usage
from base.log import logger


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


def _generation(
    node: StoredNode,
    job_costs: Mapping[int, GenerationUsage],
    check_costs: Mapping[str, GenerationUsage],
) -> GenerationUsage | None:
    """The cost of the call that wrote `node`: its chunk job's (level 1) or grouping check's."""
    if node.depth == 1:
        return None if node.job_id is None else job_costs.get(node.job_id)
    return None if node.check_key is None else check_costs.get(node.check_key)


def serve_nodes(
    nodes: Sequence[StoredNode],
    usage: MessageUsage,
    job_costs: Mapping[int, GenerationUsage],
    check_costs: Mapping[str, GenerationUsage],
    read: Sequence[datetime | None],
    blocks: Sequence[DisplayBlock],
) -> list[ServedNode]:
    """The timed nodes, finest level first then in message order, each with its two cost figures.

    A node sits on the extent of the layer-0 blocks it covers: it starts where the earliest block
    that opens at its first message starts and ends where the latest block that closes at its last
    message ends (`blocks` are `units.display_blocks`; a turn's thinking block starts at the read
    time of the message before it). A level's nodes therefore abut exactly where their spans do,
    never overlap, and a parent spans exactly its children. A message that opens or closes no block
    falls back to its read time (`units.read_times`). The stored `start_ts` / `end_ts` are only used
    to tell a timed node from an untimed one.

    A node whose span lies outside the history (an orphan: its checkpoints were rolled back or
    restored) is left out with a warning instead of failing the whole read.
    """
    opens: dict[int, datetime] = {}
    closes: dict[int, datetime] = {}
    for block in blocks:
        opens[block.i0] = min(block.start, opens.get(block.i0, block.start))
        closes[block.i1] = max(block.end, closes.get(block.i1, block.end))
    served: list[ServedNode] = []
    for node in sorted(nodes, key=lambda node: (node.depth, node.span_start)):
        if node.start_ts is None or node.end_ts is None:
            continue
        if node.span_end >= len(read):
            logger.warning(
                "understanding node {node} spans messages {first}-{last}, past the history's "
                "{count}: left out of the run timeline",
                node=node.id,
                first=node.span_start,
                last=node.span_end,
                count=len(read),
            )
            continue
        start = opens.get(node.span_start, read[node.span_start])
        end = closes.get(node.span_end, read[node.span_end])
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
                generation=_generation(node, job_costs, check_costs),
            )
        )
    return served
