"""ava.self.terminate — lifecycle scenario where an agent terminates itself.

Single LLM call within turn 1:
  llm 1 (SCRIPT[0]): ava.self.terminate() + tool_call → exec raises AgentTermination
                    → claim dispatch writes lifecycle marker + goto END → process exits

No post-process detection needed — terminate won't respawn a process (sending a chat
message for auto-resurrect is required to start a new process), so this fake is only
built once. SCRIPT length is 1.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="\u518d\u89c1\u3002",
        tool_calls=[
            {
                "id": "call_terminate",
                "name": "execute_code",
                "args": {"code": "import ava\nava.self.terminate()"},
            }
        ],
        usage_metadata=_USAGE,
    ),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=SCRIPT)
