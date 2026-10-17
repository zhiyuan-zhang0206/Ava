# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#indexed-history-windows
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime

from langchain_core.messages import AIMessage

from base.agents.history.hierarchy.units import DisplayBlock
from base.agents.history.hierarchy.usage import Usage
from base.agents.history.message_tokens import MessageTokens, PartTokens, ai_message_parts, total_of
from base.lm.content import message_addl_kwargs
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.schemas import RunTimelineUnit, RunTimelineUsage

_TURN_PART = {"thinking": "reasoning", "text": "output", "call": "tool_call"}


@dataclass(frozen=True)
class BlockTokens:
    context_tokens: int | None
    generation_tokens: int | None
    estimated: bool | None


def _first_message(view: HistoryView, block: DisplayBlock) -> int:
    return block.i0 + 1 if block.kind == "output" and _is_ai(view, block.i0) else block.i0


def _is_ai(view: HistoryView, idx: int) -> bool:
    return isinstance(view.history.messages[idx], AIMessage)


def span_tokens(view: HistoryView, span_start: int, span_end: int) -> BlockTokens:
    records = view.tokens[span_start : span_end + 1]
    if all(r.context_tokens is None for r in records):
        return BlockTokens(None, None, None)
    total = total_of(records)
    return BlockTokens(total.tokens, None, total.estimated)


@dataclass(frozen=True)
class BlockContext:
    session: int
    context_total: int | None
    request: Usage | None


_TURN_ORDER = ("thinking", "text", "call")


def _context_totals(view: HistoryView) -> tuple[dict[int, int], dict[int, int]]:
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
    def __init__(self, view: HistoryView) -> None:
        self._view = view
        self._before, self._through = _context_totals(view)
        self._kinds: dict[int, set[str]] = {}
        for block in view.units:
            if block.kind in _TURN_PART:
                self._kinds.setdefault(block.i0, set()).add(block.kind)
        self._parts: dict[int, dict[str, PartTokens]] = {}
        self._generation_parts: dict[int, dict[str, PartTokens]] = {}

    def _share(self, idx: int, *, generation: bool = False) -> dict[str, PartTokens]:
        parts = self._generation_parts if generation else self._parts
        if idx not in parts:
            msg = self._view.history.messages[idx]
            if not isinstance(msg, AIMessage):
                raise TypeError(f"turn block at message {idx} is not an AIMessage")
            record = self._view.tokens[idx]
            if generation:
                record = MessageTokens(record.generation_tokens, None, "exact")
            parts[idx] = ai_message_parts(msg, record)
        return parts[idx]

    def tokens(self, block: DisplayBlock) -> BlockTokens:
        part = _TURN_PART.get(block.kind)
        if part is not None:
            return self._turn_tokens(block.i0, part)
        records = self._view.tokens[_first_message(self._view, block) : block.i1 + 1]
        if all(r.context_tokens is None for r in records):
            return BlockTokens(None, None, None)
        total = total_of(records)
        return BlockTokens(total.tokens, None, total.estimated)

    def _turn_tokens(self, idx: int, part: str) -> BlockTokens:
        record = self._view.tokens[idx]
        if not _is_ai(self._view, idx):
            raise TypeError(f"turn block at message {idx} is not an AIMessage")
        context = self._share(idx)[part] if record.context_tokens is not None else None
        generation = (
            self._share(idx, generation=True)[part]
            if record.generation_tokens is not None
            else None
        )
        return BlockTokens(
            context.tokens if context is not None else None,
            generation.tokens if generation is not None else None,
            context.source == "estimated" if context is not None else None,
        )

    def of(self, block: DisplayBlock) -> BlockContext:
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
                share[_TURN_PART[k]].tokens for k in _TURN_ORDER[: upto + 1]
            )
        usage = view.usage.span(idx, idx)
        return BlockContext(session, total, usage if usage.calls else None)


def timeline_units(view: HistoryView, start: datetime, end: datetime) -> list[RunTimelineUnit]:
    contexts = BlockContexts(view)
    out: list[RunTimelineUnit] = []
    for unit in view.units:
        if unit.start > end or unit.end < start:
            continue
        tokens = contexts.tokens(unit)
        context = contexts.of(unit)
        inbound = (
            message_addl_kwargs(view.history.messages[unit.i0]).get("meiva_inbound_id")
            if unit.kind in ("inbound", "note")
            else None
        )
        out.append(
            RunTimelineUnit(
                kind=unit.kind,
                i0=unit.i0,
                i1=unit.i1,
                start=unit.start,
                end=unit.end,
                source=unit.source,
                inbound_id=inbound if isinstance(inbound, int) else None,
                preview=unit.preview,
                parent=None,
                context_tokens=tokens.context_tokens,
                generation_tokens=tokens.generation_tokens,
                estimated=tokens.estimated,
                session=context.session,
                context_total=context.context_total,
                request=RunTimelineUsage(**vars(context.request))
                if context.request is not None
                else None,
            )
        )
    return out
