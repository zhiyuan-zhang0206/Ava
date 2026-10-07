"""Renderable text of a message and the token estimator built on it.

Shared by the per-message true-token computation (`base/agents/history/message_tokens.py`) and
the gateway context breakdown, which use the estimator to apportion a provider-reported token
total among messages (or among parts of one message, or the sections of the system prompt). The estimator weighs CJK and other non-space
characters differently and charges a fixed framing overhead per message;
whitespace is free.

Coefficients were least-squares fitted (relative error) on the 3255 `exact`
messages of preview agent 9 (1092 tool results, 403 human turns, 1760 AI turns;
true tokens from adjacent-request `input_tokens` differences, see
`message_tokens.py`), 2026-10-08. Held-out half: median |error| 6.6% against
34% for chars/4 (tool 7.1 vs 47, human 6.3 vs 55, AI thinking 6.2 vs 25). The
sample is one largely Chinese agent on one provider; refit with new data rather
than hand-tuning.
"""

from __future__ import annotations

import re
from typing import Any, cast

from langchain_core.messages import AIMessage, BaseMessage

from base.lm.content import content_blocks

# Tokens per CJK character (kana, hangul, CJK ideographs, full-width forms).
CJK_TOKENS_PER_CHAR = 0.95
# Tokens per other non-whitespace character (latin, digits, punctuation, code).
OTHER_TOKENS_PER_CHAR = 0.36
# Fixed tokens one message costs beyond its text (role / block framing). Fitted
# apart because AI turns carry little framing (their text is the generation).
NON_AI_MESSAGE_OVERHEAD_TOKENS = 15.0
AI_MESSAGE_OVERHEAD_TOKENS = 2.0

_CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]")


def estimate_text_tokens(text: str) -> float:
    """Estimated tokens of a piece of text, without any per-message overhead
    (so parts of one message can be weighed against each other)."""
    cjk = len(_CJK.findall(text))
    other = sum(1 for ch in text if not ch.isspace()) - cjk
    return CJK_TOKENS_PER_CHAR * cjk + OTHER_TOKENS_PER_CHAR * other


def estimate_message_tokens(msg: BaseMessage) -> float:
    """Estimated context tokens of one message: its text plus framing overhead."""
    if isinstance(msg, AIMessage):
        parts = ai_message_texts(msg)
        return AI_MESSAGE_OVERHEAD_TOKENS + sum(estimate_text_tokens(t) for t in parts.values())
    return NON_AI_MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(content_text(msg.content))  # pyright: ignore[reportUnknownMemberType]


def content_text(content: object) -> str:
    """A message's renderable text. A block list (multimodal inbound, AIMessage
    content blocks) contributes only its text/thinking text — never the base64
    of an image block (that becomes image tokens, not text tokens)."""
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
    """One AIMessage's text split into reasoning / output / tool_call parts."""
    out = {"reasoning": "", "output": "", "tool_call": ""}
    content: Any = msg.content  # pyright: ignore[reportUnknownMemberType]
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
