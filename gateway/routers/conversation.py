"""Conversation snapshot endpoint — /api/agents/{agent_id}/conversation-snapshot.

One read for the selected agent's switch refresh (task #3900 batch 2): after
the detail stream re-attaches, the frontend reconciles its three conversation
read models — the head timeline window, token usage, and pending inbounds —
with a single request instead of three per-model trailing reads. The payloads
are the SAME shapes the standalone endpoints serve; this route composes them,
and those endpoints stay authoritative for first paint and their own readers.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.routers.agents_state import get_pending_messages, get_token_usage
from gateway.routers.timeline import TimelineResponse, get_timeline
from gateway.schemas.messages import PendingInbound
from gateway.schemas.stats import TokenUsageResponse

router = APIRouter()


class ConversationSnapshotResponse(BaseModel):
    """The selected agent's three conversation read models, one point in time.

    `timeline` carries the same head window `GET .../timeline` serves (no
    cursor); `token_usage` and `pending` mirror their standalone endpoints
    section-for-section (drift is caught by the side-by-side contract test).
    """

    timeline: TimelineResponse
    token_usage: TokenUsageResponse
    pending: list[PendingInbound]


@router.get(
    "/api/agents/{agent_id}/conversation-snapshot",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_conversation_snapshot(agent_id: int, request: Request) -> ConversationSnapshotResponse:
    """Compose the three conversation reads in one round trip.

    Calls the standalone routes' own functions — no duplicated logic. A
    nonexistent agent 404s through the timeline read, matching
    `GET .../timeline` (token usage and pending tolerate absence, but are not
    reached then).
    """
    return ConversationSnapshotResponse(
        timeline=get_timeline(agent_id, request, limit=None, before=None),
        token_usage=get_token_usage(agent_id, request),
        pending=get_pending_messages(agent_id, request),
    )
