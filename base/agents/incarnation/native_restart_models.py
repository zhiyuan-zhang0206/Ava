"""Guarded ACTIVE restart request, immutable acceptance and retained source facts."""

import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.messages.envelope import validate_writable_source


class NativeRestartOutcome(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    OBSERVED = "observed"
    SUPERSEDED = "superseded"
    UNCERTAIN = "uncertain"


class NativeRestartReason(StrEnum):
    TARGET_REPLACED = "target_replaced"
    RESURRECT = "resurrect"
    FORCE_TERMINATE = "force_terminate"
    INVALID_SOURCE_TRANSITION = "invalid_source_transition"
    SOURCE_COMMAND_UNAVAILABLE = "source_command_unavailable"


class NativeRestartRequest(BaseModel):
    """Validate shape without consulting mutable overlay configuration on replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    target: NativeWorkTarget
    source: str = Field(default="user", min_length=1, max_length=64)
    config_overlay: dict[str, object] | None = None

    @field_validator("config_overlay")
    @classmethod
    def _finite_overlay(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        json.dumps(value, allow_nan=False)
        return value

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        validate_writable_source(value)
        return value


class NativeRestartAcceptance(BaseModel):
    """The original source command, not a claim that restart executed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    command_id: StrictInt = Field(gt=0, lt=2**63)
    target: NativeWorkTarget
    config_overlay: dict[str, object] | None


class NativeRestartProgress(BaseModel):
    """Retained execution facts copied by the original source transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    acceptance: NativeRestartAcceptance
    outcome: NativeRestartOutcome
    applied_at: datetime | None
    observed_at: datetime | None
    reason: NativeRestartReason | None

    @model_validator(mode="after")
    def _proof_shape(self) -> "NativeRestartProgress":
        if self.observed_at is not None and self.applied_at is None:
            raise ValueError("native restart observation lacks original apply evidence")
        if self.outcome == NativeRestartOutcome.OBSERVED and self.observed_at is None:
            raise ValueError("native restart observed outcome lacks its timestamp")
        if self.outcome == NativeRestartOutcome.APPLIED and self.applied_at is None:
            raise ValueError("native restart applied outcome lacks its timestamp")
        if self.outcome in (NativeRestartOutcome.ACCEPTED, NativeRestartOutcome.SUPERSEDED) and (
            self.applied_at is not None or self.observed_at is not None
        ):
            raise ValueError("native restart no-effect outcome carries execution evidence")
        if self.outcome == NativeRestartOutcome.APPLIED and self.observed_at is not None:
            raise ValueError("native restart applied outcome has an observation")
        if (
            self.outcome in (NativeRestartOutcome.SUPERSEDED, NativeRestartOutcome.UNCERTAIN)
            and self.reason is None
        ):
            raise ValueError("native restart unresolved/no-effect outcome lacks its reason")
        return self


class NativeRestartOperation(BaseModel):
    """The versioned home-runner envelope; the gateway supplies its scoped key."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_key: str = Field(min_length=1, max_length=128)
    request: NativeRestartRequest


class NativeRestartAccepted(BaseModel):
    """The versioned runner completed its acceptance transaction, not restart."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["accepted"]
    acceptance: NativeRestartAcceptance


class NativeRestartRefused(BaseModel):
    """A typed business refusal, distinct from transport failure or acceptance."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["refused"]
    reason: Literal["identity_conflict", "invalid_overlay"]
    detail: str
