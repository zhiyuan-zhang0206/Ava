# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#indexed-history-windows
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from base.agents.history.timeline import TimelineItem, build_timeline_items
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.schemas import (
    RunTimelineMessage,
    RunTimelineMessagePart,
    RunTimelineMessages,
    RunTimelinePartKind,
)
from services.derived.insights.run_timeline.tokens import span_tokens

router = APIRouter()

_LIMIT_MAX = 200

_PART_KIND: dict[str, RunTimelinePartKind] = {
    "agent_reasoning": "think",
    "agent_chat": "text",
    "agent_code": "call",
    "code_output": "out",
    "system_marker": "note",
    "system_prompt": "prompt",
    "inbound_compact_summary": "compact",
    "inbound_compact_request": "compact",
    "inbound_chat": "inbound",
    "attach": "attach",
}


def _message(
    view: HistoryView, idx: int, items: list[TimelineItem], *, full: bool, text_max: int
) -> RunTimelineMessage:
    parts: list[RunTimelineMessagePart] = []
    for item in items:
        clipped = not full and len(item.payload) > text_max
        parts.append(
            RunTimelineMessagePart(
                kind=_PART_KIND[item.kind],
                chars=len(item.payload),
                text=item.payload[:text_max] if clipped else item.payload,
                text_truncated=clipped,
            )
        )
    stamp = next((item.created_at for item in items if item.created_at), None)
    ts = datetime.fromisoformat(stamp) if stamp else None
    tokens = span_tokens(view, idx, idx)
    return RunTimelineMessage(
        idx=idx,
        ts=ts,
        source=next((item.source for item in items if item.source), None),
        parts=parts,
        context_tokens=tokens.context_tokens,
        estimated=tokens.estimated,
    )


@router.get(
    "/api/agents/{agent_id}/run-timeline/messages",
    description="Messages ``start..end`` of the agent's stitched history, at most ``limit`` of them.",
)
def get_run_timeline_messages(
    request: Request,
    agent_id: int,
    start: Annotated[int, Query(ge=0)],
    end: Annotated[int, Query(ge=0)],
    limit: Annotated[int, Query(ge=1, le=_LIMIT_MAX)] = 50,
    *,
    full: Annotated[bool, Query()] = False,
    snapshot: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> RunTimelineMessages:
    if end < start:
        raise HTTPException(status_code=422, detail="end must not be before start")
    reader: TimelineReader = request.app.state.run_timeline_reader
    cut = reader.get(agent_id, snapshot)
    if end >= cut.count:
        raise HTTPException(404, f"message {end} not found: history has {cut.count}")
    text_max = request.app.state.config.run_timeline_message_text_max
    stop = min(end, start + limit - 1)
    out: list[RunTimelineMessage] = []
    for segment, view in reader.span(cut, start, stop):
        local_start = max(start, segment.message_start) - segment.offset
        local_stop = min(stop + 1, segment.message_end) - segment.offset
        items, _ = build_timeline_items(
            view.history.messages[local_start:local_stop],
            inputs=request.app.state.timeline_inputs,
        )
        by_message: dict[int, list[TimelineItem]] = {}
        for item in items:
            by_message.setdefault(local_start + int(item.item_id.split(".")[0]), []).append(item)
        for idx, group in sorted(by_message.items()):
            message = _message(view, idx, group, full=full, text_max=text_max)
            out.append(message.model_copy(update={"idx": idx + segment.offset}))
    return RunTimelineMessages(messages=out, next_start=stop + 1 if stop < end else None)
