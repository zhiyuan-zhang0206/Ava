from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="\u518d\u89c1\u3002",
        tool_calls=[
            {
                "id": "call_terminate",
                "name": "execute_code",
                "args": {"code": "import meiva\nmeiva.self.terminate()"},
            }
        ],
        usage_metadata=_USAGE,
    ),
)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=SCRIPT)
