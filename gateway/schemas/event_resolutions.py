"""Schemas for the authenticated class-level event-resolution API (task #1468)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EventResolutionCreate",
    "EventResolutionListResponse",
    "EventResolutionRow",
    "EventResolutionStatus",
]

EventResolutionCategory = Literal["telemetry", "log"]
EventResolutionLevel = Literal["warning", "error", "critical"]
EventResolutionStatus = Literal["dismissed", "reopened"]


class EventResolutionCreate(BaseModel):
    """One immutable event class to dismiss through the authenticated API."""

    model_config = ConfigDict(extra="forbid")

    category: EventResolutionCategory
    level: EventResolutionLevel
    event_name: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=255)
    agent_id: int | None = None
    note: str = Field(default="", max_length=4_000)

    @model_validator(mode="after")
    def _reject_per_agent_v1(self) -> EventResolutionCreate:
        """Keep class arithmetic exact until Loki counts group by agent id."""

        if self.agent_id is not None:
            raise ValueError("agent_id-specific dismissals are not supported in v1")
        return self


class EventResolutionRow(BaseModel):
    """One persisted class dismissal, including reopened history metadata."""

    model_config = ConfigDict(frozen=True)

    id: int
    category: EventResolutionCategory
    level: EventResolutionLevel
    event_name: str
    source: str
    agent_id: int | None
    dismissed_by: int
    note: str
    status: EventResolutionStatus
    dismissed_at: datetime
    reopened_at: datetime | None
    burst_count: int | None
    created_at: datetime
    updated_at: datetime


class EventResolutionListResponse(BaseModel):
    """Status-filtered resolution history for the ops agent's review cycle."""

    model_config = ConfigDict(frozen=True)

    resolutions: list[EventResolutionRow]
