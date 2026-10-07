"""Native graph work identity, separate from runtime admission protocol."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class NativeWorkPhase(StrEnum):
    PREPARING = "preparing"
    ACTIVE = "active"
    SETTLED = "settled"
    ABANDONED = "abandoned"
    UNCERTAIN = "uncertain"


class NativeCancelOutcome(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    RECOVERED_STOPPED = "recovered_stopped"
    UNCERTAIN = "uncertain"


class NativeWorkTarget(BaseModel):
    """The immutable original work and actual hosted owner observed by a caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    work_id: UUID
    agent_id: StrictInt = Field(gt=0, lt=2**63)
    machine: str = Field(min_length=1)
    generation: UUID
    owner: UUID
    protocol: Literal[1]

    @field_validator("protocol", mode="before")
    @classmethod
    def protocol_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("native work protocol must be an integer")
        return value


class NativeCancelMarker(BaseModel):
    """A halted checkpoint's exact command and original work attribution."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    command_id: UUID
    target: NativeWorkTarget


class NativeCancelAcceptance(BaseModel):
    """Immutable command acceptance; it does not claim execution completed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    command_id: UUID
    target: NativeWorkTarget


class NativeWorkUncertainError(RuntimeError):
    """An accepted native command lacks proof permitting subsequent work."""


class NativeCancelPendingError(RuntimeError):
    """Generic claim must leave its batch untouched for this original work."""

    def __init__(self, marker: NativeCancelMarker) -> None:
        super().__init__("native cancel must settle before claiming another batch")
        self.marker = marker
