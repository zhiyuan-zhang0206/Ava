"""Token counts of a timeline block, from the per-message counts of the history view.

A message block (inbound, note, output) sums the counts of its messages. A block that is a part of
one AIMessage's turn (thinking, text, call) takes that part's share of the message, by the estimator
(`ai_message_parts`), so it is estimated unless the message has a single part.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage

from base.agents.history.hierarchy.units import DisplayBlock
from base.agents.history.message_tokens import MessageTokens, ai_message_parts, total_of
from gateway.run_timeline.history import HistoryView

# The AIMessage part each turn block shows.
_TURN_PART = {"thinking": "reasoning", "text": "output", "call": "tool_call"}


@dataclass(frozen=True)
class BlockTokens:
    context_tokens: int | None
    generation_tokens: int | None
    estimated: bool | None


def block_tokens(view: HistoryView, block: DisplayBlock) -> BlockTokens:
    """The context and generation tokens of `block`."""
    part = _TURN_PART.get(block.kind)
    if part is not None:
        return _turn_tokens(view, block.i0, part)
    first = block.i0 + 1 if block.kind == "output" and _is_ai(view, block.i0) else block.i0
    records = view.tokens[first : block.i1 + 1]
    total = total_of(records)
    if all(r.context_tokens is None for r in records):
        return BlockTokens(None, None, None)
    return BlockTokens(total.tokens, None, total.estimated)


def _is_ai(view: HistoryView, idx: int) -> bool:
    return isinstance(view.history.messages[idx], AIMessage)


def _turn_tokens(view: HistoryView, idx: int, part: str) -> BlockTokens:
    msg = view.history.messages[idx]
    record = view.tokens[idx]
    if not isinstance(msg, AIMessage):
        raise TypeError(f"turn block at message {idx} is not an AIMessage")
    context = ai_message_parts(msg, record)[part] if record.context_tokens is not None else None
    generation = (
        ai_message_parts(msg, MessageTokens(record.generation_tokens, None, "exact"))[part]
        if record.generation_tokens is not None
        else None
    )
    if context is None and generation is None:
        return BlockTokens(None, None, None)
    return BlockTokens(
        context.tokens if context is not None else None,
        generation.tokens if generation is not None else None,
        context.source == "estimated" if context is not None else None,
    )
