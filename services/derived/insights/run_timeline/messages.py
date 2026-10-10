"""Raw messages of the stitched history — the read behind a node's or unit's "expand".

``GET /api/agents/{id}/run-timeline/messages?start=&end=`` returns messages
``start..end`` (inclusive stitched indices, the spans nodes and units carry),
each split into its parts by the same projection the console timeline serves
(`base.agents.history.timeline.build_timeline_items`). A range longer than
``limit`` messages is cut and ``next_start`` says where to continue. A part
longer than ``display.run_timeline_message_text_max`` is clipped and flagged;
``full=true`` returns it uncut.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from base.agents.history.timeline import TimelineItem, build_timeline_items
from services.derived.insights.run_timeline.history import HistoryView, HistoryViewCache
from services.derived.insights.run_timeline.schemas import (
    RunTimelineMessage,
    RunTimelineMessagePart,
    RunTimelineMessages,
    RunTimelinePartKind,
)
from services.derived.insights.run_timeline.tokens import span_tokens

router = APIRouter()

_LEGACY_TS_FLOOR = datetime(2020, 1, 1, tzinfo=UTC)
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
        ts=ts if ts is not None and ts >= _LEGACY_TS_FLOOR else None,
        source=next((item.source for item in items if item.source), None),
        parts=parts,
        context_tokens=tokens.context_tokens,
        estimated=tokens.estimated,
    )


@router.get("/api/agents/{agent_id}/run-timeline/messages")
def get_run_timeline_messages(
    request: Request,
    agent_id: int,
    start: Annotated[int, Query(ge=0)],
    end: Annotated[int, Query(ge=0)],
    limit: Annotated[int, Query(ge=1, le=_LIMIT_MAX)] = 50,
    full: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> RunTimelineMessages:
    """Messages ``start..end`` of the agent's stitched history, at most ``limit`` of them."""
    if end < start:
        raise HTTPException(status_code=422, detail="end must not be before start")
    views: HistoryViewCache = request.app.state.run_timeline_views
    view = views.get(request.app.state.db, agent_id)
    messages = view.history.messages
    if end >= len(messages):
        raise HTTPException(
            status_code=404, detail=f"message {end} not found: history has {len(messages)}"
        )
    text_max = request.app.state.config.run_timeline_message_text_max
    stop = min(end, start + limit - 1)
    items, _ = build_timeline_items(
        messages[start : stop + 1], [], inputs=request.app.state.timeline_inputs
    )
    by_message: dict[int, list[TimelineItem]] = {}
    for item in items:
        by_message.setdefault(start + int(item.item_id.split(".")[0]), []).append(item)
    return RunTimelineMessages(
        messages=[
            _message(view, idx, group, full=full, text_max=text_max)
            for idx, group in sorted(by_message.items())
        ],
        next_start=stop + 1 if stop < end else None,
    )
