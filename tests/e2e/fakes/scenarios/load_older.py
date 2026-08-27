"""Long-history scenario — enough turns to exceed the timeline tail window
(DEFAULT_TIMELINE_LIMIT=50), so scroll-up history loading has a previous page
to fetch (has_more=true).

13 turns, each thinking + narration + a trivial execute_code tool call → the
real exec runs → ~4 timeline items per turn (agent_reasoning, agent_chat,
agent_code, code_output); the final turn replies only. One user message drives
the whole script (the agent graph loops the SCRIPT per inbound).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def _turn(i: int) -> AIMessage:
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": f"\u7b2c {i} \u6b65\u601d\u8003\u3002", "index": 0},
            {"type": "text", "text": f"\u6267\u884c\u7b2c {i} \u6b65\u3002", "index": 1},
        ],
        tool_calls=[
            {
                "id": f"call_{i}",
                "name": "execute_code",
                "args": {"code": f"print({i})"},
            }
        ],
        usage_metadata=_USAGE,
    )


LOAD_OLDER_SCRIPT: tuple[AIMessage, ...] = (
    *(_turn(i) for i in range(1, 14)),
    AIMessage(content="\u5168\u90e8\u6267\u884c\u5b8c\u6bd5\u3002", usage_metadata=_USAGE),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=LOAD_OLDER_SCRIPT)
