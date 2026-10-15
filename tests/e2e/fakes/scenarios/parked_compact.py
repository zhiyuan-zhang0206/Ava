from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

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


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=PARKED_COMPACT_SCRIPT)
