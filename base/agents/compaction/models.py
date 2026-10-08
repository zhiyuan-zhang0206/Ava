"""Immutable source observation, prepared summary and cold application attribution."""

import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from base.agents.history.closing_request import ClosingRequest
from base.agents.incarnation.native_work_models import NativeWorkTarget, NativeWorkUncertainError


class CompactConflictError(ValueError):
    """A compact key, source history or exact receiver no longer matches."""


class CompactHeldError(NativeWorkUncertainError):
    """The original compact executor or application lacks sufficient proof."""


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompactTarget(Record):
    """A new host's retained observation of actual ended, resource-closed work."""

    protocol: StrictInt
    observation_id: UUID
    source: NativeWorkTarget
    checkpoint_id: str = Field(min_length=1)
    checkpoint_ns: str = Field(max_length=0)
    messages_version: str = Field(min_length=1)
    compact_channel_version: str | None
    segment_version: StrictInt = Field(ge=0)
    model: str = Field(min_length=1)

    @field_validator("protocol")
    @classmethod
    def supported_protocol(cls, value: int) -> int:
        if value != 1:
            raise ValueError("guarded compact requires explicit producer protocol 1")
        return value


class CompactOutcome(StrEnum):
    ACCEPTED = "accepted"
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    NOOP = "noop"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class CompactAcceptance(Record):
    """Original business acceptance, never proof of application or provider completion."""

    command_id: UUID
    target: CompactTarget


class PreparedSummary(Record):
    """Durable result needed to apply the original summary without another model call."""

    attempt_id: UUID
    summary: str = Field(min_length=1)
    message_id: UUID
    created_at: datetime
    closing: ClosingRequest | None = None

    @field_validator("summary")
    @classmethod
    def usable_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prepared compact summary must contain text")
        return value

    @field_validator("created_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("prepared compact timestamp must be timezone-aware")
        return value

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class CompactMarker(Record):
    """One application transition bound to its original source and prepared result."""

    acceptance: CompactAcceptance
    execution: NativeWorkTarget
    attempt_id: UUID
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_version: StrictInt = Field(ge=1)


class CompactStatus(Record):
    acceptance: CompactAcceptance
    outcome: CompactOutcome
    reason: str | None
    checkpoint_id: str | None
    recovery_checkpoint_id: str | None
    attempt_id: UUID | None
    attempt_provider: str | None
    execution: NativeWorkTarget | None
    result_available: bool
    continuation_released: bool
