"""Full message-flow scenario — one user turn with reasoning + code + output + reply.

Script:
  turn 1: thinking block + narration + execute_code tool call (the e2e agent
          really executes `print(1 + 2)` → code_output "3")
  turn 2: final reply text

The e2e agent runs its real graph, so one user message fans out into the full
timeline item chain — agent_reasoning (thinking block), agent_code (tool
call), code_output (exec result), agent_chat (reply) — exercising the
streaming SSE path and the committed snapshot in one turn.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

REPLY_TEXT = "结果是 3。"

MESSAGE_FLOW_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content=[
            {"type": "thinking", "thinking": "用户问 1+2 等于几，写代码算最稳。", "index": 0},
            {"type": "text", "text": "我来算一下。", "index": 1},
        ],
        tool_calls=[
            {
                "id": "call_sum",
                "name": "execute_code",
                "args": {"code": "print(1 + 2)"},
            }
        ],
        usage_metadata=_USAGE,
    ),
    AIMessage(content=REPLY_TEXT, usage_metadata=_USAGE),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=MESSAGE_FLOW_SCRIPT)
