from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import (
    ReplySuggestionConfigurationError,
    ReplySuggestionService,
)
from gateway.http.disconnect import run_until_disconnect
from gateway.http.middleware.error_envelope import error_response

router = APIRouter()
_log = logging.getLogger(__name__)


@router.post(
    "/api/agents/{agent_id}/reply-suggestion",
    dependencies=[Depends(deny_isolated_result_read)],
    response_model=ReplySuggestionResponse,
    description="Suggest replies when helping the user respond would be useful.\n\n200 with an empty `suggestions` array and a `reason` when there is nothing to show: the\nagent is mid-turn, a reply suggestion is not useful, or the gate or draft\nmodel failed (logged as a warning). 404 for an unknown agent; 503 when the conversation\ncannot be read. The work stops when the client disconnects.",
)
async def post_reply_suggestion(
    agent_id: int, request: Request
) -> ReplySuggestionResponse | JSONResponse:

    service: ReplySuggestionService = request.app.state.reply_suggestions
    try:
        result = await run_until_disconnect(service.suggest(agent_id), request.is_disconnected)
    except ReplySuggestionConfigurationError:
        _log.exception("Reply suggestion model configuration is invalid")
        return error_response(
            request,
            code="reply_suggestion_unavailable",
            status=503,
            detail="Reply suggestion models are not configured correctly.",
            retryable=False,
        )

    return (
        result
        if result is not None
        else ReplySuggestionResponse(suggestions=[], reason="client_disconnected")
    )
