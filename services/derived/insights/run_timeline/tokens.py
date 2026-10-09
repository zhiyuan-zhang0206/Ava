"""Token counts of a timeline block, from the per-message counts of the history view.

A message block (inbound, note, output) sums the counts of its messages. A block that is a part of
one AIMessage's turn (thinking, text, call) takes that part's share of the message, by the estimator
(`ai_message_parts`), so it is estimated unless the message has a single part.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime

from langchain_core.messages import AIMessage

from base.agents.history.hierarchy.units import DisplayBlock
from base.agents.history.hierarchy.usage import Usage
from base.agents.history.message_tokens import MessageTokens, ai_message_parts, total_of
from services.derived.insights.run_timeline.history import HistoryView

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
    records = view.tokens[_first_message(view, block) : block.i1 + 1]
    total = total_of(records)
    if all(r.context_tokens is None for r in records):
        return BlockTokens(None, None, None)
    return BlockTokens(total.tokens, None, total.estimated)


def _first_message(view: HistoryView, block: DisplayBlock) -> int:
    """The first message a message block stands for: an output block starts at its AIMessage, whose own tokens belong to the turn blocks."""
    return block.i0 + 1 if block.kind == "output" and _is_ai(view, block.i0) else block.i0


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


def span_tokens(view: HistoryView, span_start: int, span_end: int) -> BlockTokens:
    """The context tokens of the messages `span_start`..`span_end` (inclusive), summed."""
    records = view.tokens[span_start : span_end + 1]
    if all(r.context_tokens is None for r in records):
        return BlockTokens(None, None, None)
    total = total_of(records)
    return BlockTokens(total.tokens, None, total.estimated)


@dataclass(frozen=True)
class MessageBar:
    """One message as the context rows draw it: where it sits (the extent of its blocks) and its weights."""

    idx: int
    start: datetime
    end: datetime
    session: int
    context_tokens: int
    estimated: bool
    context_total: int
    request: Usage | None


def _context_totals(view: HistoryView) -> dict[int, int]:
    """For each message, the context a request would carry through it: its session's head and every
    message up to it, each at the weight it was read with. Before an AIMessage this is the
    `input_tokens` of the request that produced it. A message no request has read is absent."""
    totals: dict[int, int] = {}
    for session, segment in enumerate(view.segments):
        running = segment.head.context_tokens or 0 if segment.head is not None else 0
        start = view.history.segment_starts[session]
        for offset, record in enumerate(segment.messages):
            if record.context_tokens is None:
                continue
            running += record.context_tokens
            totals[start + offset] = running
    return totals


def message_bars(view: HistoryView) -> list[MessageBar]:
    """Every message a request has read, with the extent of the block(s) that show it.

    A turn block (thinking, text, call) shows its AIMessage; any other block its messages after the
    AIMessage that opened it. A message's extent is the union of its blocks'. A message no request has
    read has no count and no bar. `request` is the AIMessage's own LLM request (its usage), if any.
    """
    extent: dict[int, tuple[datetime, datetime]] = {}
    for block in view.units:
        first = block.i0 if block.kind in _TURN_PART else _first_message(view, block)
        last = block.i0 if block.kind in _TURN_PART else block.i1
        for idx in range(first, last + 1):
            start, end = extent.get(idx, (block.start, block.end))
            extent[idx] = (min(start, block.start), max(end, block.end))
    totals = _context_totals(view)
    starts = view.history.segment_starts
    bars: list[MessageBar] = []
    for idx in sorted(extent):
        record = view.tokens[idx]
        if record.context_tokens is None or idx not in totals:
            continue
        call = view.usage.span(idx, idx)
        bars.append(
            MessageBar(
                idx,
                *extent[idx],
                max(bisect_right(starts, idx) - 1, 0),
                record.context_tokens,
                record.source == "estimated",
                totals[idx],
                call if call.calls else None,
            )
        )
    return bars
