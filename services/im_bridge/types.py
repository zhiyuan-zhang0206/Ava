"""Shared IM Bridge message, session, and adapter types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypedDict


@dataclass
class Reply:
    """One outbound message. ``buttons`` are tap targets the adapter may
    render (Telegram inline keyboard); ``markdown`` marks agent content that
    may carry markdown (adapters render it, others ignore)."""

    text: str
    buttons: list[tuple[str, str]] | None = None  # (label, command)
    markdown: bool = False


@dataclass
class SpawnDraft:
    """The /spawn menu's in-flight selections (preset -> model -> effort).
    Lives on ChatState; cleared when the spawn executes."""

    preset_id: int | None = None
    preset_name: str | None = None
    preset_label: str | None = None
    model: str | None = None
    effort: str | None = None


class AgentRow(TypedDict):
    """One row of GET /api/agents — the fields the IM surface reads."""

    agent_id: int
    label: str | None
    status: str
    machine: str | None
    spawned_at: str | None
    started_at: str | None
    last_active_at: str | None
    last_inbound_at: str | None
    pid: int | None


@dataclass
class InboundMessage:
    """One normalized message from a platform adapter."""

    channel: str  # "telegram" | "weixin" | "feishu"
    chat_id: str  # platform-scoped conversation id
    text: str
    message_id: str | None = None


@dataclass
class ChatState:
    """Per (channel, chat) session state, in-memory only."""

    channel: str
    chat_id: str
    current_agent_id: int | None = None
    spawn_draft: SpawnDraft | None = None


class IMAdapter(ABC):
    """Contract every IM channel adapter implements.

    The adapter only transports: normalize platform events into
    :class:`InboundMessage`, hand them to ``core.handle_inbound``, and render
    ``send`` calls back onto the platform. No agent logic lives here.
    """

    channel: str = ""

    def __init__(self, core: Any) -> None:
        # Any: adapters are also constructed with test doubles; the core
        # contract (async handle_inbound / adapters dict) is duck-typed.
        self.core = core

    @abstractmethod
    async def start(self) -> None:
        """Connect and start receiving. Deliver messages via
        ``await self.core.handle_inbound(InboundMessage(...))``.
        Reconnect with backoff on failure."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect and clean up."""

    can_buttons: bool = False
    """True when the platform renders inline buttons (Telegram)."""

    can_type: bool = False
    """True when the platform has a native \"typing\" indicator."""

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        """Send text to a chat, splitting over the platform cap.

        ``buttons`` are optional tap targets ((label, command) pairs) the
        platform may render; ``markdown`` marks agent content that may carry
        markdown. Raise on failure (core logs and retries once)."""

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        """Send ``text`` to the user's private chat on this channel.

        The outbound-notification entry point (``IMBridgeCore.notify_user``):
        each adapter resolves its own owner chat (Telegram owner id, WeChat
        account user, Feishu's last p2p sender). Adapters that cannot resolve
        one raise NotImplementedError — ``notify_user`` catches and skips
        them, so a channel without a known chat never breaks the fan-out.
        """

        raise NotImplementedError(f"{type(self).__name__} cannot resolve an owner chat")

    async def typing(self, chat_id: str) -> None:
        """Show the platform's native \"typing\" indicator (Telegram's
        bouncing dots) for a moment. Core refreshes it until the agent
        replies; the base implementation refuses — check ``can_type`` first."""

        raise NotImplementedError(f"{type(self).__name__} cannot show typing")
