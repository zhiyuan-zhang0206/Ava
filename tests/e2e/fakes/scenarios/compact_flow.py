"""Compact-flow scenario — UI-triggered (force) compact with backend summary.

Script (one shared fake instance; the compaction summary call consumes the
NEXT turn — agent/graph/_claim_dispatch.py `_handle_compact_request` calls
`generate_summary(state.messages, ctx.llm)` with the agent's own LLM):
  turn 1: reply to the user's first message (gives the conversation content)
  turn 2: the compaction summary text (consumed by the Compaction LLM call)
  turn 3: the post-compact narration — the compact transition resumes at LLM
          (resume=LLM), so the model speaks once after the wipe
  turn 4: reply to the post-compact message (proves the agent still works)

The compact wipes state.messages and replaces them with the summary tail — a
clean wipe (REMOVE_ALL + ContextReset), so the post-compact timeline is
[system_prompt, inbound_compact_request] and the pre-compact chat/reply are
gone. The force path stamps the summary message with ava_msg_type
=compact_request (Task #1017 fix, PR #1796) — the timeline must render it as
the inbound_compact_request envelope, never the unrecognized alarm.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

FIRST_REPLY = "\u6536\u5230\uff0c\u6211\u5728\u3002"
SUMMARY_TEXT = "\u5bf9\u8bdd\u603b\u7ed3\uff1a\u7528\u6237\u95ee\u597d\uff0c\u6211\u56de\u590d\u4e86\u3002\u4e4b\u540e\u4e0a\u4e0b\u6587\u88ab\u538b\u7f29\u3002"
POST_COMPACT_NARRATION = "\u4e0a\u4e0b\u6587\u5df2\u538b\u7f29\uff0c\u7ee7\u7eed\u3002"
POST_COMPACT_REPLY = "compact \u4e4b\u540e\u6211\u8fd8\u5728\u3002"

COMPACT_FLOW_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content=FIRST_REPLY, usage_metadata=_USAGE),
    AIMessage(content=SUMMARY_TEXT, usage_metadata=_USAGE),
    AIMessage(content=POST_COMPACT_NARRATION, usage_metadata=_USAGE),
    AIMessage(content=POST_COMPACT_REPLY, usage_metadata=_USAGE),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=COMPACT_FLOW_SCRIPT)
