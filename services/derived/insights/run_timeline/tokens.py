"""Token counts of a timeline block, from the per-message counts of the history view.

A message block (inbound, note, output) sums the counts of its messages. A block that is a part of
one AIMessage's turn (thinking, text, call) takes that part's share of the message, by the estimator
(`ai_message_parts`), so it is estimated unless the message has a single part.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

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
class BlockContext:
    """Where a block sits in the context: its compaction segment, the context through it, and the LLM request it belongs to."""

    session: int
    context_total: int | None
    request: Usage | None


_TURN_ORDER = ("thinking", "text", "call")


def _context_totals(view: HistoryView) -> tuple[dict[int, int], dict[int, int]]:
    """For each message a request has read: the context before it and through it, in its session
    (the head and every message up to it, each at the weight it was read with). Before an AIMessage
    this is the `input_tokens` of the request that produced it."""
    before: dict[int, int] = {}
    through: dict[int, int] = {}
    for session, segment in enumerate(view.segments):
        running = segment.head.context_tokens or 0 if segment.head is not None else 0
        start = view.history.segment_starts[session]
        for offset, record in enumerate(segment.messages):
            if record.context_tokens is None:
                continue
            before[start + offset] = running
            running += record.context_tokens
            through[start + offset] = running
    return before, through


class BlockContexts:
    """The context rows' view of a history: the context through each block, per block.

    A message block (inbound, note, output) is the context through its last message. The blocks of
    one AIMessage's turn (thinking, text, call, in time order) each add their share of the message
    (`ai_message_parts`, the same split the block's own tokens use) to the context before the
    message, so the first block starts from the request's `input_tokens` and the last one ends at
    the context through the whole message.
    """

    def __init__(self, view: HistoryView) -> None:
        self._view = view
        self._before, self._through = _context_totals(view)
        self._kinds: dict[int, set[str]] = {}
        for block in view.units:
            if block.kind in _TURN_PART:
                self._kinds.setdefault(block.i0, set()).add(block.kind)
        self._parts: dict[int, dict[str, int]] = {}

    def _share(self, idx: int) -> dict[str, int]:
        if idx not in self._parts:
            msg = self._view.history.messages[idx]
            if not isinstance(msg, AIMessage):
                raise TypeError(f"turn block at message {idx} is not an AIMessage")
            parts = ai_message_parts(msg, self._view.tokens[idx])
            self._parts[idx] = {kind: part.tokens for kind, part in parts.items()}
        return self._parts[idx]

    def of(self, block: DisplayBlock) -> BlockContext:
        """The context of `block`; its total is None while no request has read its messages."""
        view = self._view
        session = max(bisect_right(view.history.segment_starts, block.i0) - 1, 0)
        if block.kind not in _TURN_PART:
            return BlockContext(session, self._through.get(block.i1), None)
        idx = block.i0
        if idx not in self._through:
            return BlockContext(session, None, None)
        present = [kind for kind in _TURN_ORDER if kind in self._kinds[idx]]
        if block.kind == present[-1]:
            total = self._through[idx]
        else:
            share = self._share(idx)
            upto = _TURN_ORDER.index(block.kind)
            total = self._before[idx] + sum(
                share.get(_TURN_PART[k], 0) for k in _TURN_ORDER[: upto + 1]
            )
        usage = view.usage.span(idx, idx)
        return BlockContext(session, total, usage if usage.calls else None)
