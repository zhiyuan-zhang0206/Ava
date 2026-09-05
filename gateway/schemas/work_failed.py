"""Request and delivery-result schemas for failure feedback."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FailureStage = Literal["ci", "qa", "merge"]
FailureDeliveryKind = Literal["author", "author_resurrected", "delegator", "task_alert"]


class WorkFailedIn(BaseModel):
    """One producer-owned failure fact; identity is supplied by commit parsing."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=255)
    commit_sha: str = Field(min_length=1, max_length=64)
    stage: FailureStage
    summary: str = Field(min_length=1, max_length=2000)
    author_agent_id: int = Field(gt=0)
    dedup_key: str = Field(min_length=1, max_length=255)


class WorkFailedResult(BaseModel):
    """The durable event identity and the one routing outcome it reached."""

    model_config = ConfigDict(frozen=True)

    event_id: int
    status: Literal["delivered", "task_alerted", "duplicate"]
    delivered_to: str | None
    delivery_kind: FailureDeliveryKind | None
