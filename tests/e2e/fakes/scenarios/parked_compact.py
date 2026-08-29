"""Parked-compact scenario — multi-message history compressed while parked.

Like compact_flow, but with THREE pre-compact exchanges so the pre-compact
history outlives the post-compact timeline by several item_ids. The task #1959
regression needs that: with a single exchange the post-compact items
(system_prompt / head / compact envelope / narration) reuse the SAME item_ids
as the wiped ones, the keep-all switch-back merge dedupes them by id, and the
resurrection the reset window exists to prevent never becomes visible.

Script (one shared fake instance):
  turn 1-3: replies to the three user messages (the history to compress)
  turn 4:   the compaction summary text (consumed by the Compaction LLM call)
  turn 5:   the post-compact narration (the compact transition resumes at LLM)

Post-compact, state.messages is [system_prompt, head, summary envelope,
narration]; the pre-compact exchanges at higher item_ids are gone from the
REST snapshot — a keep-all merge of the stale parked bucket would resurrect
them (exactly the bug).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

REPLY_1 = "\u7b2c\u4e00\u6761\u56de\u590d\u3002"
REPLY_2 = "\u7b2c\u4e8c\u6761\u56de\u590d\u3002"
REPLY_3 = "\u7b2c\u4e09\u6761\u56de\u590d\u3002"
SUMMARY_TEXT = (
    "\u5bf9\u8bdd\u603b\u7ed3\uff1a\u7528\u6237\u8fde\u53d1\u4e09\u6761\u6d88\u606f"
    "\uff0c\u6211\u5404\u56de\u590d\u4e86\u4e00\u6761\u3002"
)
POST_COMPACT_NARRATION = "\u4e0a\u4e0b\u6587\u5df2\u538b\u7f29\uff0c\u7ee7\u7eed\u3002"

PARKED_COMPACT_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content=REPLY_1, usage_metadata=_USAGE),
    AIMessage(content=REPLY_2, usage_metadata=_USAGE),
    AIMessage(content=REPLY_3, usage_metadata=_USAGE),
    AIMessage(content=SUMMARY_TEXT, usage_metadata=_USAGE),
    AIMessage(content=POST_COMPACT_NARRATION, usage_metadata=_USAGE),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=PARKED_COMPACT_SCRIPT)
