# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#indexed-history-windows
from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.units import display_blocks, divide_units, read_times
from base.agents.history.hierarchy.usage import MessageUsage, Usage
from base.agents.history.timeline_inputs import TimelineReadInputs
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.model import (
    Fact,
    Projection,
    Segment,
    Totals,
)
from services.derived.insights.run_timeline.tokens import timeline_units


def segment_view(
    history: FullHistory,
    floor: datetime | None,
    *,
    inputs: TimelineReadInputs,
) -> HistoryView:
    read = read_times(history.messages, timeline_inputs=inputs)
    read = [max(ts, floor) if ts is not None and floor is not None else ts or floor for ts in read]
    blocks = display_blocks(
        divide_units(history.messages, timeline_inputs=inputs),
        history.messages,
        read,
    )
    return HistoryView.of(history, blocks, MessageUsage(history.messages), read)


def _facts(view: HistoryView, start: int, offset: int, before: Totals) -> list[Fact]:
    opens: dict[int, datetime] = {}
    closes: dict[int, datetime] = {}
    for block in view.units:
        opens[block.i0] = min(block.start, opens.get(block.i0, block.start))
        closes[block.i1] = max(block.end, closes.get(block.i1, block.end))
    out: list[Fact] = []
    base = before
    usage = MessageUsage(
        view.history.messages[start:],
        preceding=Usage(
            before.calls,
            before.input,
            before.cache_read,
            before.output,
            before.cache_write,
            before.cost_usd,
            before.cost_calls,
        ),
    )
    context, known, estimated = base.context, base.known, base.estimated
    for idx in range(start, len(view.history.messages)):
        msg, record = view.history.messages[idx], view.tokens[idx]
        context += record.context_tokens or 0
        known += record.context_tokens is not None
        estimated += record.context_tokens is not None and record.source != "exact"
        after = Totals(
            **vars(usage.prefix(idx - start + 1)),
            context=context,
            known=known,
            estimated=estimated,
        )
        sent = (view.read[idx - 1] if idx else None) or view.read[idx]
        out.append(
            Fact(
                idx=idx + offset,
                start_ts=opens.get(idx, view.read[idx]),
                end_ts=closes.get(idx, view.read[idx]),
                request_ts=sent if isinstance(msg, AIMessage) and msg.usage_metadata else None,
                before_totals=before,
                after_totals=after,
            )
        )
        before = after
    return out


def project_segment(
    checkpoint_id: str,
    history: FullHistory,
    previous: Segment | None,
    *,
    inputs: TimelineReadInputs,
) -> Projection:
    session = previous.session + 1 if previous is not None else 0
    first = previous.message_end if previous is not None else 0
    local_start = history.segment_starts[0] if session and history.segment_starts else 0
    floor = previous.read_after if previous is not None else None
    view = segment_view(history, floor, inputs=inputs)
    offset = first - local_start
    facts = _facts(view, local_start, offset, previous.totals if previous else Totals())
    extent = view.extent
    blocks = timeline_units(view, *extent) if extent else []
    blocks = [
        b.model_copy(
            update={
                "i0": b.i0 + offset,
                "i1": b.i1 + offset,
                "session": session,
            }
        )
        for b in blocks
    ]
    segment = Segment(
        session=session,
        checkpoint_id=checkpoint_id,
        message_start=first,
        message_end=first + len(history.messages) - local_start,
        local_start=local_start,
        read_before=floor,
        read_after=view.read[-1] if view.read else floor,
        start_ts=extent[0] if extent else None,
        end_ts=extent[1] if extent else None,
        totals=facts[-1].after_totals if facts else previous.totals if previous else Totals(),
    )
    return Projection(segment, facts, blocks, view)
