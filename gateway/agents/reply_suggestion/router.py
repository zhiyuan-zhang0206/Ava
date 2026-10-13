"""`POST /api/agents/{agent_id}/reply-suggestion`: the drafted reply for the agent's last message."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import ReplySuggestionService
from gateway.http.disconnect import run_until_disconnect

router = APIRouter()


@router.post(
    "/api/agents/{agent_id}/reply-suggestion",
    dependencies=[Depends(deny_isolated_result_read)],
)
async def post_reply_suggestion(agent_id: int, request: Request) -> ReplySuggestionResponse:
    """Draft the reply the user is likely to send to the agent's last message.

    200 with `suggestion` null and a `reason` for every way there is nothing to show: the
    agent is mid-turn, its last message is not waiting for the user, or the gate or draft
    model failed (logged as a warning). 404 for an unknown agent; 503 when the conversation
    cannot be read. The work stops when the client disconnects.
    """
    service: ReplySuggestionService = request.app.state.reply_suggestions
    result = await run_until_disconnect(service.suggest(agent_id), request.is_disconnected)
    # A disconnected client never reads this body.
    return result if result is not None else ReplySuggestionResponse(reason="client_disconnected")
