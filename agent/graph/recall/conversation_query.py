"""The recent conversation as one piece of text: the query both recall passes key on.

Passive memory recall searches the note pool with it and skill recall scores the skill
catalog against it, so the two judge the same window of the conversation. One owner keeps
them from drifting apart on what counts as the conversation and how much of it is read.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from base.agents.messages.kwargs import AvaMsgType, read_ava_kwargs
from base.lm.reasoning import content_blocks

# How many recent conversation messages form the query.
QUERY_MESSAGES = 6
# Bound on the query text so a single huge paste does not become the query.
QUERY_CHAR_CAP = 2000


def _message_text(msg: AnyMessage) -> str:
    """Best-effort plain text of a message's content (str or multimodal list)."""
    content: Any = msg.content  # pyright: ignore[reportUnknownMemberType]
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content_blocks(content):
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(
            text := cast(dict[str, Any], block).get("text"), str
        ):
            parts.append(text)
    return " ".join(parts)


def build_conversation_query(messages: Collection[AnyMessage]) -> str:
    """Concatenate the last few genuine conversation messages into a query.

    Genuine = agent replies (AIMessage) and inbound chat (HumanMessage tagged
    `ava_msg_type='inbound'`). Framework system-notes (heartbeat, lifecycle
    markers, and prior recall notes themselves) are excluded so the query
    reflects the conversation, not our own injections -- otherwise recall would
    feed on its own output.
    """
    picked: list[str] = []
    for msg in reversed(list(messages)):
        if isinstance(msg, AIMessage) or (
            isinstance(msg, HumanMessage)
            and read_ava_kwargs(msg).get("ava_msg_type") == AvaMsgType.INBOUND
        ):
            text = _message_text(msg)
        else:
            continue
        text = text.strip()
        if text:
            picked.append(text)
        if len(picked) >= QUERY_MESSAGES:
            break
    picked.reverse()
    return "\n".join(picked)[-QUERY_CHAR_CAP:]
