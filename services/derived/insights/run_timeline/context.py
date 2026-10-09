"""The context of one LLM request — ``GET /api/agents/{agent_id}/run-timeline/context?at=``.

A run spans several compaction segments (sessions); the composer's context breakdown only
answers for the last request of the latest one. This reads the same breakdown for any request of
the stitched history: the request is the first one at or after message index ``at`` (the last one
when none follows), and its context is what the agent really sent — the segment's own head
(system prompt) and every message of the segment before the request's AIMessage — bucketed by
the same breakdown the composer's panel uses, from each message's own token count.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from langchain_core.messages import AIMessage

from base.agents.history.context_breakdown import request_breakdown
from base.agents.history.context_response import context_breakdown_response
from services.derived.insights.run_timeline.history import HistoryView, HistoryViewCache
from services.derived.insights.run_timeline.schemas import RunTimelineContext

router = APIRouter()


@dataclass(frozen=True)
class LlmRequest:
    """One LLM request: the AIMessage at `idx`, sent at `ts` (the read time of the message before it) in compaction segment `session`."""

    idx: int
    ts: datetime
    session: int


def llm_requests(view: HistoryView) -> list[LlmRequest]:
    """The agent's LLM requests in message order; a request with no placeable time is left out."""
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
    """The first request at or after message `at`, else the last one."""
    if not requests:
        return None
    found = bisect_left([r.idx for r in requests], at)
    return requests[min(found, len(requests) - 1)]


@router.get("/api/agents/{agent_id}/run-timeline/context")
def get_run_timeline_context(
    request: Request, agent_id: int, at: Annotated[int, Query(ge=0)]
) -> RunTimelineContext:
    """The context breakdown of the LLM request at (or next after) message index `at`."""
    views: HistoryViewCache = request.app.state.run_timeline_views
    view = views.get(request.app.state.db, agent_id)
    found = request_at(llm_requests(view), at)
    if found is None:
        raise HTTPException(status_code=404, detail="the agent has made no LLM request")
    history = view.history
    start = history.segment_starts[found.session]
    breakdown = context_breakdown_response(
        request.app.state.db_pool,
        agent_id,
        request_breakdown(
            history.segment_heads[found.session],
            history.messages[start : found.idx + 1],
            view.segments[found.session],
            found.idx - start,
        ),
        catalog=request.app.state.catalog,
    )
    return RunTimelineContext(
        **breakdown.model_dump(),
        request=found.idx,
        session=found.session,
        sessions=len(view.history.segment_starts),
        ts=found.ts,
    )
