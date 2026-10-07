"""Versioned retry-launch admission and repeatable reconciliation wire contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RetryLaunchRequest(BaseModel):
    """One deliberate retry of the attempt the caller actually observed."""

    model_config = ConfigDict(extra="forbid")
    expected_prior_attempt_id: UUID


class RetryLaunchAccepted(BaseModel):
    """Historical committed intent; neither a wake nor execution acknowledgement."""

    agent_id: int = Field(gt=0)
    prior_attempt_id: UUID
    launch_attempt_id: UUID
    accepted_at: datetime
    accepted: Literal[True] = True
    execution_observed: Literal[False] = False


class LaunchReconcileRequest(BaseModel):
    """Only a committed retry receipt can authorize this versioned wake."""

    model_config = ConfigDict(extra="forbid")
    launch_attempt_id: UUID


class LaunchReconciled(BaseModel):
    """A wake hint result, never evidence that the first turn executed."""

    wake_published: bool
