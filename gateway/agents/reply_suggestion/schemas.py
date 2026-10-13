"""Wire model of `POST /api/agents/{agent_id}/reply-suggestion`."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

MAX_SUGGESTION_CHARS = 500
ReplyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUGGESTION_CHARS)
]

SuggestionReason = Literal[
    "disabled",
    "agent_not_idle",
    "no_agent_text",
    "not_useful",
    "gate_failed",
    "generation_failed",
    "invalid_generation",
    "client_disconnected",
]


class DraftReplies(BaseModel):
    """Structured model output, validated before candidates reach the composer."""

    model_config = ConfigDict(extra="forbid")

    suggestions: list[ReplyText] = Field(min_length=1)

    @field_validator("suggestions")
    @classmethod
    def distinct_replies(cls, replies: list[str]) -> list[str]:
        if len({reply.casefold() for reply in replies}) != len(replies):
            raise ValueError("Reply candidates must be distinct")
        return replies


class ReplySuggestionResponse(BaseModel):
    """Drafted replies, or the reason there are none.

    `suggestions` is nonempty exactly when `reason` is None. Every non-error outcome is a 200: a
    missing suggestion is an ordinary result, never a failure of the request.
    """

    suggestions: list[ReplyText]
    reason: SuggestionReason | None = None
