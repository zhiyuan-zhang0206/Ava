"""Immutable timeline delivery requests and their durable lifecycle."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from services.entrypoints.im_bridge.cursor_store import PushWatermark


class OutboundStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class TimelineAdapterKind(StrEnum):
    TELEGRAM = "telegram-v1"
    FEISHU = "feishu-v1"
    WEIXIN = "weixin-v1"


class TimelineSourceKind(StrEnum):
    MESSAGE = "message"
    INBOUND = "inbound"


class TimelineChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    fallback_text: str | None = None
    html: bool = False


class PreparedTimelineSend(BaseModel):
    """Credential-free adapter-owned rendering, frozen before acceptance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter_kind: TimelineAdapterKind
    account_id: str = Field(min_length=1)
    chunks: tuple[TimelineChunk, ...] = Field(min_length=1)
    markdown: bool
    buttons: None = None  # Timeline dialog has no command/menu buttons.


class TimelineSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: TimelineSourceKind
    identity: str = Field(min_length=1)
    block_idx: int = Field(ge=0)


def timeline_source(item: dict[str, Any]) -> TimelineSource | None:
    """A qualified source; never positional item_id, text or timestamp."""
    block = item.get("source_block_idx")
    if not isinstance(block, int) or isinstance(block, bool) or block < 0:
        return None
    message = item.get("source_message_id")
    if isinstance(message, str) and message:
        return TimelineSource(kind=TimelineSourceKind.MESSAGE, identity=message, block_idx=block)
    inbound = item.get("source_inbound_id")
    if isinstance(inbound, int) and not isinstance(inbound, bool) and inbound > 0:
        return TimelineSource(
            kind=TimelineSourceKind.INBOUND, identity=str(inbound), block_idx=block
        )
    return None


class TimelineIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str
    chat_id: str
    agent_id: int
    source: TimelineSource
    prepared: PreparedTimelineSend
    replay_id: str = ""


class OutboundIdentityConflictError(ValueError):
    """An existing source/replay identity has another immutable request."""


class OutboundAccountMismatchError(ValueError):
    """Regular acceptance cannot silently rebind an existing recipient cursor."""


@dataclass(frozen=True)
class TimelineCandidate:
    item: dict[str, Any]
    intent: TimelineIntent | None


class TimelineAcceptance(NamedTuple):
    intent_ids: tuple[int, ...]
    watermark: PushWatermark | None
    blocked: bool = False
    selected_agent_id: int | None = None
