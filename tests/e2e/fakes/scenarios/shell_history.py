from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

_TALL_OUTPUT = 'print("\\n".join(f"row {i:03d}" for i in range(300)))'

_SHELL_SETUP = (
    "import meiva\n"
    'sid = meiva.shell.sessions.new("history-probe", ttl=1800)\n'
    'meiva.shell.sessions.send(sid, "seq 1 400")\n'
    'meiva.shell.sessions.send(sid, "for i in $(seq 1 600); do echo tick-$i; sleep 1; done")\n'
    'print(f"SHELL_SID={sid}")\n'
)

SHELL_HISTORY_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="Filling the timeline.",
        tool_calls=[{"id": "call_tall", "name": "execute_code", "args": {"code": _TALL_OUTPUT}}],
        usage_metadata=_USAGE,
    ),
    AIMessage(
        content="Creating the shell session.",
        tool_calls=[{"id": "call_shell", "name": "execute_code", "args": {"code": _SHELL_SETUP}}],
        usage_metadata=_USAGE,
    ),
    AIMessage(content="DONE", usage_metadata=_USAGE),
)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=SHELL_HISTORY_SCRIPT)
