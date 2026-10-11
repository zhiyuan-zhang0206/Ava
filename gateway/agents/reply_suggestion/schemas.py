"""Wire model of `POST /api/agents/{agent_id}/reply-suggestion`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SuggestionReason = Literal[
    "disabled",
    "agent_not_idle",
    "no_agent_text",
    "not_awaiting_reply",
    "gate_failed",
    "generation_failed",
    "invalid_generation",
    "client_disconnected",
]


class ReplySuggestionResponse(BaseModel):
    """A drafted reply, or the reason there is none.

    `suggestion` is set exactly when `reason` is None. Every non-error outcome is a 200: a
    missing suggestion is an ordinary result, never a failure of the request.
    """

    suggestion: str | None = None
    reason: SuggestionReason | None = None
