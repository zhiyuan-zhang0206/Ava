"""Feishu public SDK event payloads normalized at the IM boundary."""

import json
from collections.abc import Collection
from typing import Any

from services.entrypoints.im_bridge.adapters import feishu_poll_cursor as cursors
from services.entrypoints.im_bridge.types import InboundMessage


def normalize_message(data: Any, seen_messages: Collection[str]) -> InboundMessage | None:
    """Map a ``P2ImMessageReceiveV1`` (or duck-typed stand-in) to an
    InboundMessage; return None for events we do not bridge."""
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    if message is None or sender is None:
        return None
    if getattr(message, "chat_type", "") != "p2p":
        return None  # group chats are not bridged
    if getattr(message, "message_type", "") != "text":
        return None  # only plain text is bridged
    if getattr(sender, "sender_type", "") != "user":
        return None  # the bot's own messages must not echo back into core
    content = getattr(message, "content", "") or ""
    payload: dict[str, Any] = json.loads(content)
    text = payload["text"].strip()
    if not text:
        return None
    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", "") if sender_id is not None else ""
    if not open_id:
        return None
    message_id = getattr(message, "message_id", None)
    # The polling fallback may have already fed this message (or vice
    # versa): a shared seen-set makes the two paths idempotent.
    if message_id in seen_messages:
        return None
    return InboundMessage(
        channel="feishu",
        chat_id=open_id,  # contract: the feishu session IS the user's open_id
        text=text,
        message_id=message_id,
        idempotency_key=cursors.idempotency_key(message_id),
    )


def normalize_card_action(data: Any) -> InboundMessage | None:
    action = getattr(data, "event", None)
    if action is None:
        return None
    operator = getattr(action, "operator", None)
    open_id = getattr(operator, "open_id", "") if operator is not None else ""
    if not open_id:
        return None
    card_action: Any = getattr(action, "action", None)
    if card_action is None:
        return None
    card_value: dict[str, object] = getattr(card_action, "value", None) or {}
    key = str(card_value.get("key", ""))
    if not key:
        return None
    return InboundMessage(channel="feishu", chat_id=open_id, text=key)
