"""The context of one LLM request — ``GET /api/agents/{agent_id}/run-timeline/context?at=``.

A run spans several compaction segments (sessions); the composer's context breakdown only
answers for the last request of the latest one. This reads the same breakdown for any request of
the stitched history: the request is the first one at or after message index ``at`` (the last one
when none follows), and its context is what the agent really sent — the segment's own head
(system prompt) and every message of the segment before the request's AIMessage — bucketed by
the same `compute_breakdown` the composer's panel uses, anchored to that request's provider
`input_tokens`. The same module lists the requests (`llm_requests`) the timeline's context-size
row draws.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.messages import AIMessage, BaseMessage

from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.state import context_breakdown_response
from gateway.run_timeline.history import HistoryView, HistoryViewCache
from gateway.run_timeline.schemas import RunTimelineContext, RunTimelineRequest

router = APIRouter()


def llm_requests(view: HistoryView) -> list[RunTimelineRequest]:
    """The agent's LLM requests in message order; a request with no placeable time is left out."""
    starts = view.history.segment_starts
    out: list[RunTimelineRequest] = []
    for idx, msg in enumerate(view.history.messages):
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            continue
        sent = view.read[idx - 1] if idx > 0 and view.read[idx - 1] is not None else view.read[idx]
        if sent is None:
            continue
        out.append(
            RunTimelineRequest(
                idx=idx,
                ts=sent,
                session=max(bisect_right(starts, idx) - 1, 0),
                input_tokens=int(msg.usage_metadata["input_tokens"]),
            )
        )
    return out


def request_input(view: HistoryView, request: RunTimelineRequest) -> list[BaseMessage]:
    """What the agent sent for `request`: its segment's head, then the segment's messages before it."""
    history = view.history
    head = history.segment_heads[request.session]
    body = history.messages[history.segment_starts[request.session] : request.idx]
    return [head, *body] if head is not None else body


def request_at(requests: list[RunTimelineRequest], at: int) -> RunTimelineRequest | None:
    """The first request at or after message `at`, else the last one."""
    if not requests:
        return None
    found = bisect_left([r.idx for r in requests], at)
    return requests[min(found, len(requests) - 1)]


@router.get(
    "/api/agents/{agent_id}/run-timeline/context",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_run_timeline_context(
    request: Request, agent_id: int, at: Annotated[int, Query(ge=0)]
) -> RunTimelineContext:
    """The context breakdown of the LLM request at (or next after) message index `at`."""
    views: HistoryViewCache = request.app.state.run_timeline_views
    view = views.get(request.app.state.db, agent_id)
    found = request_at(llm_requests(view), at)
    if found is None:
        raise HTTPException(status_code=404, detail="the agent has made no LLM request")
    breakdown = context_breakdown_response(
        request, agent_id, request_input(view, found), found.input_tokens
    )
    return RunTimelineContext(
        **breakdown.model_dump(),
        request=found.idx,
        session=found.session,
        sessions=len(view.history.segment_starts),
        ts=found.ts,
    )
