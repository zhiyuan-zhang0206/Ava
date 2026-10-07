"""Typed closure evidence returned by the existing resource admission rule."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from base.agents.incarnation.resources import ResourceProcess


class ResourceTransferReason(StrEnum):
    EXACT_HOST_EXIT = "exact_host_exit"
    LIFECYCLE_RECEIPT = "lifecycle_receipt"


class ResourceTransferProof(BaseModel):
    """One successful admission's empty, unfrozen managed predecessor proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: int
    source_generation: UUID
    source_owner: UUID
    target_generation: UUID
    target_owner: UUID
    predecessor_process: ResourceProcess | None
    successor_process: ResourceProcess
    reason: ResourceTransferReason
    lifecycle_command_id: int | None = None

    @model_validator(mode="after")
    def evidence_shape(self) -> "ResourceTransferProof":
        if self.reason is ResourceTransferReason.EXACT_HOST_EXIT:
            if self.predecessor_process is None or self.lifecycle_command_id is not None:
                raise ValueError("exact host exit requires only predecessor process evidence")
        elif self.lifecycle_command_id is None or self.lifecycle_command_id <= 0:
            raise ValueError("lifecycle transfer requires an original command receipt")
        return self
