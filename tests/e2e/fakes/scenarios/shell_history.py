"""Shell-monitor history-state scenario -- a tall timeline plus a long terminal.

Turn 1 prints a 300-row block (the timeline needs vertical range so a
scroll-position assertion has somewhere to go); turn 2 creates one of the
agent's persistent shell sessions and fills it with 400 numbered lines plus a
`SHELL_SID=<id>` marker the test parses to address the monitor page. Turn 3
is the final reply that tells the test the script is done.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

_TALL_OUTPUT = 'print("\\n".join(f"row {i:03d}" for i in range(300)))'

_SHELL_SETUP = (
    "import ava\n"
    'sid = ava.shell.sessions.new("history-probe", ttl=1800)\n'
    'ava.shell.sessions.send(sid, "seq 1 400")\n'
    'ava.shell.sessions.send(sid, "for i in $(seq 1 600); do echo tick-$i; sleep 1; done")\n'
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


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=SHELL_HISTORY_SCRIPT)
