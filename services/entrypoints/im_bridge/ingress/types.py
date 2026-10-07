"""Frozen provider source identity and honest business outcomes."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IngressStatus(StrEnum):
    RETAINED = "retained"
    CLAIMED = "claimed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    QUARANTINED = "quarantined"


class IngressRouteKind(StrEnum):
    CHAT = "chat"
    COMMAND = "command"
    NOTICE_REPLY = "notice_reply"
    TERMINAL = "terminal"


class ProviderSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    namespace: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    message_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")

    @field_validator("message_id")
    @classmethod
    def valid_uint64(cls, value: str) -> str:
        if int(value) >= 2**64:
            raise ValueError("Weixin message id exceeds uint64")
        return value


class IngressRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: IngressRouteKind
    text: str
    agent_id: int | None = Field(default=None, gt=0)
    notice_id: int | None = Field(default=None, gt=0)
    spawn_draft: dict[str, str | int | None] | None = None
    command_account: str | None = None
    reason: str | None = None


class IngressReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(gt=0)
    source: ProviderSource
    route: IngressRoute
    status: IngressStatus
    attempt_id: UUID | None
    inbound_id: int | None = Field(default=None, gt=0)
    outcome_reason: str | None = None
    result: dict[str, object] | None = None


class IngressBindingState(StrEnum):
    HELD = "held"
    DRAINING = "draining"
    ACTIVE = "active"


class PollBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str
    account_id: str
    epoch: UUID
    cursor: str
    state: IngressBindingState

    @property
    def held(self) -> bool:
        return self.state == IngressBindingState.HELD


class IngressIdentityConflictError(ValueError):
    """A provider source already identifies another immutable message."""


class StalePollBindingError(RuntimeError):
    """This account generation no longer owns poll admission or checkpoint."""
