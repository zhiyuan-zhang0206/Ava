from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

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


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=COMPACT_FLOW_SCRIPT)
