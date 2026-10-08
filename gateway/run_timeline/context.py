"""The context of one LLM request — ``GET /api/agents/{agent_id}/run-timeline/context?at=``.

A run spans several compaction segments (sessions); the composer's context breakdown only
answers for the last request of the latest one. This reads the same breakdown for any request of
the stitched history: the request is the first one at or after message index ``at`` (the last one
when none follows), and its context is what the agent really sent — the segment's own head
(system prompt) and every message of the segment before the request's AIMessage — bucketed by
the same breakdown the composer's panel uses, from each message's own token count. The same module
lists the requests (`llm_requests`) the timeline's context-size row draws.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.messages import AIMessage

from base.agents.history.message_tokens import total_of
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.history.context_breakdown import request_breakdown
from gateway.agents.state import context_breakdown_response
from gateway.run_timeline.history import HistoryView, HistoryViewCache
from gateway.run_timeline.schemas import RunTimelineContext, RunTimelineRequest

router = APIRouter()


def llm_requests(view: HistoryView) -> list[RunTimelineRequest]:
    """The agent's LLM requests in message order; a request with no placeable time is left out."""
    starts = view.history.segment_starts
    out: list[RunTimelineRequest] = []
    previous: int | None = None  # the last usage-bearing AIMessage, whether or not it is listed
    for idx, msg in enumerate(view.history.messages):
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            continue
        session = max(bisect_right(starts, idx) - 1, 0)
        # What this request read for the first time: since the previous request of its session, or since the session began.
        first = starts[session] if previous is None or previous < starts[session] else previous
        previous = idx
        added = total_of(view.tokens[first:idx])
        sent = view.read[idx - 1] if idx > 0 and view.read[idx - 1] is not None else view.read[idx]
        if sent is None:
            continue
        out.append(
            RunTimelineRequest(
                idx=idx,
                ts=sent,
                session=session,
                input_tokens=int(msg.usage_metadata["input_tokens"]),
                output_tokens=int(msg.usage_metadata["output_tokens"]),
                added_tokens=added.tokens,
                added_estimated=added.estimated,
            )
        )
    return out


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
    history = view.history
    start = history.segment_starts[found.session]
    breakdown = context_breakdown_response(
        request,
        agent_id,
        request_breakdown(
            history.segment_heads[found.session],
            history.messages[start : found.idx + 1],
            view.segments[found.session],
            found.idx - start,
        ),
    )
    return RunTimelineContext(
        **breakdown.model_dump(),
        request=found.idx,
        session=found.session,
        sessions=len(view.history.segment_starts),
        ts=found.ts,
    )
