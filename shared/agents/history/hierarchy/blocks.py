"""Block units — fold the console item stream into the engine's level-0 units.

Frozen rule (Q7 evidence, 2026-09-17; task #3704): the level-0 unit stream is
`shared.agents.history.timeline.build_timeline_items`' item stream folded back into block
spans:
- items of the same AI message (agent_reasoning / agent_chat / agent_code)
  extend the current block; items of a new AI message close it and open one;
- code_output items extend the current block (tool results fold into the
  "think -> act -> observe" cycle they answer);
- inbound_chat closes the block and forms a standalone one;
- compact summary / request items close the block (they are trigger points —
  see `seal.py`);
- system_prompt / system_marker / attach items are transparent.

The fold reproduces the pilot `blocks_and_triggers` partition exactly; it was
verified on 8 real checkpoint segments across 3 agents (Q7 evidence record in
the task workspace).
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.agents.history.timeline import TimelineItem

AGENT_ITEM_KINDS = frozenset({"agent_chat", "agent_code", "agent_reasoning"})
COMPACT_ITEM_KINDS = frozenset({"inbound_compact_summary", "inbound_compact_request"})


@dataclass(frozen=True)
class Block:
    """One level-0 unit: an inclusive message-index span and its opening kind."""

    i0: int
    i1: int
    kind: str  # ai | human | tool (tool only when a run starts with an orphan output)


def fold_blocks(items: list[TimelineItem]) -> list[Block]:
    """Fold items into block spans; see the module docstring for the rule."""
    blocks: list[Block] = []
    cur_start: int | None = None
    cur_end: int | None = None
    cur_kind = ""
    cur_msg = -1  # message index of the item last folded into the open block

    def close() -> None:
        nonlocal cur_start, cur_end, cur_msg
        if cur_start is not None and cur_end is not None:
            blocks.append(Block(i0=cur_start, i1=cur_end, kind=cur_kind))
        cur_start = cur_end = None
        cur_msg = -1

    for item in items:
        msg_idx = int(item.item_id.split(".")[0])
        kind = item.kind
        if kind in AGENT_ITEM_KINDS:
            if cur_start is not None and cur_msg == msg_idx:
                cur_end = msg_idx
            else:
                close()
                cur_start, cur_end, cur_kind, cur_msg = msg_idx, msg_idx, "ai", msg_idx
        elif kind == "code_output":
            if cur_start is None:
                cur_start, cur_end, cur_kind, cur_msg = msg_idx, msg_idx, "tool", msg_idx
            else:
                cur_end = msg_idx
                cur_msg = msg_idx
        elif kind == "inbound_chat":
            close()
            blocks.append(Block(i0=msg_idx, i1=msg_idx, kind="human"))
        elif kind in COMPACT_ITEM_KINDS:
            close()
        # system_prompt / system_marker / attach: transparent
    close()
    return blocks
