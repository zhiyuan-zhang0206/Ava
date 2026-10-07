"""Immutable IM delivery requests and their durable lifecycle."""

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


class OutboundAdapterKind(StrEnum):
    TELEGRAM = "telegram-v1"
    FEISHU = "feishu-v1"
    WEIXIN = "weixin-v1"


class OutboundSourceKind(StrEnum):
    MESSAGE = "message"
    INBOUND = "inbound"
    NOTICE = "notice"


class OutboundChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    fallback_text: str | None = None
    html: bool = False


class PreparedOutboundSend(BaseModel):
    """Credential-free adapter-owned rendering, frozen before acceptance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter_kind: OutboundAdapterKind
    account_id: str = Field(min_length=1)
    chunks: tuple[OutboundChunk, ...] = Field(min_length=1)
    markdown: bool
    buttons: tuple[tuple[str, str], ...] | None = None


class OutboundSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OutboundSourceKind
    identity: str = Field(min_length=1)
    block_idx: int = Field(ge=0)


def timeline_source(item: dict[str, Any]) -> OutboundSource | None:
    """A qualified source; never positional item_id, text or timestamp."""
    block = item.get("source_block_idx")
    if not isinstance(block, int) or isinstance(block, bool) or block < 0:
        return None
    message = item.get("source_message_id")
    if isinstance(message, str) and message:
        return OutboundSource(kind=OutboundSourceKind.MESSAGE, identity=message, block_idx=block)
    inbound = item.get("source_inbound_id")
    if isinstance(inbound, int) and not isinstance(inbound, bool) and inbound > 0:
        return OutboundSource(
            kind=OutboundSourceKind.INBOUND, identity=str(inbound), block_idx=block
        )
    return None


class OutboundIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str
    chat_id: str
    agent_id: int
    source: OutboundSource
    prepared: PreparedOutboundSend
    replay_id: str = ""


class OutboundIdentityConflictError(ValueError):
    """An existing source/replay identity has another immutable request."""


class OutboundAccountMismatchError(ValueError):
    """Regular acceptance cannot silently rebind an existing recipient cursor."""


@dataclass(frozen=True)
class TimelineCandidate:
    item: dict[str, Any]
    intent: OutboundIntent | None


class TimelineAcceptance(NamedTuple):
    intent_ids: tuple[int, ...]
    watermark: PushWatermark | None
    blocked: bool = False
    selected_agent_id: int | None = None


class NoticePollDecision(StrEnum):
    QUEUED = "queued"
    FILTERED = "filtered"


class NoticePollImportReason(StrEnum):
    LEGACY_CURSOR = "legacy_cursor"
    NO_HISTORY = "no_history"
    LEGACY_HISTORY_UNKNOWN = "legacy_history_unknown"


class NoticePollReceipt(NamedTuple):
    decision: NoticePollDecision
    intent_ids: tuple[int, ...]
