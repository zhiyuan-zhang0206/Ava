"""Char counts of a message's renderable text — the unit of the chars/4 token estimate.

Shared by the gateway context breakdown (bucketed by kind) and the per-message
true-token computation (`base/agents/history/message_tokens.py`), so both
estimate the same way.
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import AIMessage

from base.lm.content import content_blocks


def text_chars(content: object) -> int:
    """Char count of a message's renderable text. A block list (multimodal
    inbound, AIMessage content blocks) counts only its text/thinking text — never
    the base64 of an image block (that becomes image tokens, not char tokens, and
    would wildly inflate a chars/4 estimate)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content_blocks(cast(list[Any], content)):
            if isinstance(b, dict):
                d = cast(dict[str, Any], b)
                if isinstance(text := d.get("text"), str):
                    total += len(text)
                elif isinstance(thinking := d.get("thinking"), str):
                    total += len(thinking)
            elif isinstance(b, str):
                total += len(b)
        return total
    return len(str(content))


def ai_message_chars(msg: AIMessage) -> dict[str, int]:
    """Split one AIMessage's chars into reasoning / output / tool_call buckets."""
    out = {"reasoning": 0, "output": 0, "tool_call": 0}
    content: Any = msg.content  # pyright: ignore[reportUnknownMemberType]
    if isinstance(content, str):
        out["output"] += len(content)
    elif isinstance(content, list):
        for b in content_blocks(cast(list[Any], content)):
            if not isinstance(b, dict):
                continue
            d = cast(dict[str, Any], b)
            if isinstance(thinking := d.get("thinking"), str):
                out["reasoning"] += len(thinking)
            elif b.get("type") == "text" and isinstance(text := b.get("text"), str):
                out["output"] += len(text)
    for tc in msg.tool_calls:
        if isinstance(code := tc["args"].get("code"), str):
            out["tool_call"] += len(code)
    return out
