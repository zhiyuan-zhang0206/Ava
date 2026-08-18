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

FIRST_REPLY = "收到，我在。"
SUMMARY_TEXT = "对话总结：用户问好，我回复了。之后上下文被压缩。"
POST_COMPACT_NARRATION = "上下文已压缩，继续。"
POST_COMPACT_REPLY = "compact 之后我还在。"

COMPACT_FLOW_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content=FIRST_REPLY, usage_metadata=_USAGE),
    AIMessage(content=SUMMARY_TEXT, usage_metadata=_USAGE),
    AIMessage(content=POST_COMPACT_NARRATION, usage_metadata=_USAGE),
    AIMessage(content=POST_COMPACT_REPLY, usage_metadata=_USAGE),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=COMPACT_FLOW_SCRIPT)
