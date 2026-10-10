"""Which chat inbound anchors a timeline render consumes.

``build_timeline_items`` aligns timeline ts with the agent's ``kind='chat'``
inbound rows only for messages that predate their own id or read time. This
module decides, from the messages alone, which rows a render can consume so a
cold reader fetches those instead of the agent's whole inbound history.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from langchain_core.messages import BaseMessage, SystemMessage

from base.agents.messages.kwargs import AvaMsgType, kwargs_read_time, read_ava_kwargs


class ChatAnchorDemand(NamedTuple):
    """Which chat anchors a ``build_timeline_items`` render can consume.

    ``referenced_ids`` are looked up exactly (``ava_inbound_id``);
    ``positional`` means an id-less legacy inbound consumes anchors in order
    from the agent's oldest chat row.
    """

    referenced_ids: list[int]
    positional: bool


def chat_anchor_demand(messages: Sequence[BaseMessage]) -> ChatAnchorDemand | None:
    """The anchors rendering *messages* depends on, or None when it ignores them.

    Anchors only reach the output through an id-less inbound (positional
    ``inbound_id``) or a message without a real read time (synthetic ts
    offset from the latest anchor). A render with neither is identical with
    ``[]`` anchors. Unlike ``needs_chat_anchors`` this also covers legacy
    non-inbound messages after a modern inbound, so cold reads stay exact.
    """
    referenced_ids: list[int] = []
    positional = False
    depends = False
    for msg in messages:
        if isinstance(msg, SystemMessage):
            continue
        kwargs = read_ava_kwargs(msg)
        if kwargs_read_time(kwargs) is None:
            depends = True
        if kwargs.get("ava_msg_type") != AvaMsgType.INBOUND:
            continue
        if "ava_inbound_id" not in kwargs:
            positional = depends = True
        elif type(embedded_id := kwargs["ava_inbound_id"]) is int:
            # Malformed ids are left to rendering, which rejects them.
            referenced_ids.append(embedded_id)
    return ChatAnchorDemand(referenced_ids, positional) if depends else None
