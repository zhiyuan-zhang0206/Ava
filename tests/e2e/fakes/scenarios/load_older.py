from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

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
    *(_turn(i) for i in range(1, 36)),
    AIMessage(content="\u5168\u90e8\u6267\u884c\u5b8c\u6bd5\u3002", usage_metadata=_USAGE),
)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=LOAD_OLDER_SCRIPT)
