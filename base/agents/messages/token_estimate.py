# meiva:ref base/agents/messages/docs/token-estimation.meiva.okf.md#unicode-token-weight
from __future__ import annotations

import re
from typing import Any, cast

from langchain_core.messages import AIMessage, BaseMessage

from base.lm.reasoning import content_blocks

CJK_TOKENS_PER_CHAR = 0.95

OTHER_TOKENS_PER_CHAR = 0.36


NON_AI_MESSAGE_OVERHEAD_TOKENS = 15.0
AI_MESSAGE_OVERHEAD_TOKENS = 2.0

_CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]")


def estimate_text_tokens(text: str) -> float:
    cjk = len(_CJK.findall(text))

    other = sum(map(len, text.split())) - cjk
    return CJK_TOKENS_PER_CHAR * cjk + OTHER_TOKENS_PER_CHAR * other


def estimate_message_tokens(msg: BaseMessage) -> float:
    if isinstance(msg, AIMessage):
        parts = ai_message_texts(msg)
        return AI_MESSAGE_OVERHEAD_TOKENS + sum(estimate_text_tokens(t) for t in parts.values())
    return NON_AI_MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(content_text(msg.content))


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content_blocks(cast(list[Any], content)):
            if isinstance(b, dict):
                d = cast(dict[str, Any], b)
                if isinstance(text := d.get("text"), str):
                    parts.append(text)
                elif isinstance(thinking := d.get("thinking"), str):
                    parts.append(thinking)
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return str(content)


def ai_message_texts(msg: AIMessage) -> dict[str, str]:
    out = {"reasoning": "", "output": "", "tool_call": ""}
    content: Any = msg.content
    if isinstance(content, str):
        out["output"] += content
    elif isinstance(content, list):
        for b in content_blocks(cast(list[Any], content)):
            if not isinstance(b, dict):
                continue
            d = cast(dict[str, Any], b)
            if isinstance(thinking := d.get("thinking"), str):
                out["reasoning"] += thinking
            elif b.get("type") == "text" and isinstance(text := b.get("text"), str):
                out["output"] += text
    for tc in msg.tool_calls:
        if isinstance(code := tc["args"].get("code"), str):
            out["tool_call"] += code
    return out
