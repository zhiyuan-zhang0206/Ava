"""Contract tests for the level-0 block fold (`shared/hierarchy/blocks.py`).

The fold rule is frozen by the Q7 equivalence evidence (task #3704): it must
reproduce the pilot `blocks_and_triggers` partition on the console item
stream. These tests lock the rule's edges: same-message continuation, tool
results folding into their cycle, marker transparency, trigger closure.
"""

from __future__ import annotations

from typing import Literal

from shared.hierarchy.blocks import Block, fold_blocks
from shared.timeline import TimelineItem

ItemKind = Literal[
    "inbound_chat",
    "inbound_compact_summary",
    "inbound_compact_request",
    "attach",
    "agent_chat",
    "agent_code",
    "agent_reasoning",
    "code_output",
    "system_prompt",
    "system_marker",
]


def it(m: int, b: int, kind: ItemKind) -> TimelineItem:
    return TimelineItem(item_id=f"{m}.{b}", kind=kind, payload="x")


def test_ai_cycle_merges_tool_output() -> None:
    """One AI message plus its tool results = one block; the next opens one."""
    items = [
        it(3, 0, "agent_reasoning"),
        it(3, 1, "agent_chat"),
        it(4, 0, "code_output"),
        it(5, 0, "agent_reasoning"),
    ]
    assert fold_blocks(items) == [Block(3, 4, "ai"), Block(5, 5, "ai")]


def test_inbound_is_standalone_and_compact_closes() -> None:
    """Inbound closes the open block and stands alone; compact/markers are not units."""
    items = [
        it(3, 0, "agent_chat"),
        it(4, 0, "inbound_chat"),
        it(5, 0, "system_marker"),
        it(6, 0, "inbound_compact_summary"),
        it(7, 0, "agent_reasoning"),
    ]
    assert fold_blocks(items) == [Block(3, 3, "ai"), Block(4, 4, "human"), Block(7, 7, "ai")]


def test_orphan_output_opens_tool_block() -> None:
    """A run starting with an orphan tool output still forms a unit."""
    items = [it(0, 0, "code_output"), it(1, 0, "agent_reasoning")]
    assert fold_blocks(items) == [Block(0, 0, "tool"), Block(1, 1, "ai")]


def test_same_message_extended_across_interleaved_items() -> None:
    """Items of one AI message continue the block across an interleaved output."""
    items = [
        it(2, 0, "agent_reasoning"),
        it(2, 1, "agent_code"),
        it(3, 0, "code_output"),
        it(4, 0, "agent_reasoning"),
    ]
    assert fold_blocks(items) == [Block(2, 3, "ai"), Block(4, 4, "ai")]


def test_markers_are_transparent_between_blocks() -> None:
    """A marker between two AI messages neither closes nor merges them."""
    items = [it(1, 0, "agent_chat"), it(2, 0, "system_marker"), it(3, 0, "agent_chat")]
    assert fold_blocks(items) == [Block(1, 1, "ai"), Block(3, 3, "ai")]


def test_empty_stream() -> None:
    assert fold_blocks([]) == []
