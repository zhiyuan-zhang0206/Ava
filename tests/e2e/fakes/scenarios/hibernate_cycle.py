"""Hibernation swap-out → wake cross-process scenario.

One plain-text turn, replayed once per process. The first process answers the
first chat and idles; the test then sends the real swap-out signal (SIGUSR1),
the process exits cleanly and its row parks 'hibernating'; a second chat makes the
restarter's hibernation controller swap the agent back in (a REAL new process,
same agent_id), which restores the checkpoint and answers the second chat with a
fresh model (script cursor back at 0).

`build` returns the same one-element script for every process — each process
handles exactly one chat (one turn), so the single element is consumed once per
process and never exhausts.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

# A plain-text turn (no tool_calls) → the agent answers and halts back to idle.
SCRIPT: tuple[AIMessage, ...] = (AIMessage(content="ack", usage_metadata=_USAGE),)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=SCRIPT)
