# ava:ref services/derived/insights/run_timeline/docs/range-projection.ava.okf.md#indexed-history-windows
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from langchain_core.messages import AIMessage

from base.agents.history.context_breakdown import request_breakdown
from base.agents.history.context_response import context_breakdown_response
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.schemas import RunTimelineContext

router = APIRouter()


@dataclass(frozen=True)
class LlmRequest:
    idx: int
    ts: datetime
    session: int


def llm_requests(view: HistoryView) -> list[LlmRequest]:
    starts = view.history.segment_starts
    out: list[LlmRequest] = []
    for idx, msg in enumerate(view.history.messages):
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            continue
        sent = view.read[idx - 1] if idx > 0 and view.read[idx - 1] is not None else view.read[idx]
        if sent is not None:
            out.append(LlmRequest(idx, sent, max(bisect_right(starts, idx) - 1, 0)))
    return out


def request_at(requests: list[LlmRequest], at: int) -> LlmRequest | None:
    if not requests:
        return None
    found = bisect_left([r.idx for r in requests], at)
    return requests[min(found, len(requests) - 1)]


@router.get(
    "/api/agents/{agent_id}/run-timeline/context",
    description="The context breakdown of the LLM request at (or next after) message index `at`.",
)
def get_run_timeline_context(
    request: Request,
    agent_id: int,
    at: Annotated[int, Query(ge=0)],
    snapshot: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> RunTimelineContext:
    reader: TimelineReader = request.app.state.run_timeline_reader
    cut = reader.get(agent_id, snapshot)
    idx, ts, segment, view = reader.request(cut, at)
    local = idx - segment.offset
    history = view.history
    body_start = history.segment_starts[0]
    breakdown = context_breakdown_response(
        request.app.state.db_pool,
        agent_id,
        request_breakdown(
            history.segment_heads[0],
            history.messages[body_start : local + 1],
            view.segments[0],
            local - body_start,
        ),
        catalog=request.app.state.catalog,
        default_model_reader=request.app.state.default_model_reader,
    )
    return RunTimelineContext(
        **breakdown.model_dump(),
        request=idx,
        session=segment.session,
        sessions=cut.sessions,
        ts=ts,
    )
