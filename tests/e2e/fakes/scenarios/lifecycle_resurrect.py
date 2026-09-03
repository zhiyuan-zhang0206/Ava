"""ava.self.terminate + auto-resurrect — lifecycle scenario where sending a message to
a dead agent automatically resurrects it.

Two SCRIPTs built across processes, distinguished by the 'resurrect' kind row in the
inbound_messages table (auto-resurrect triggered by deliver_chat_inbound; resurrect_agent is
the only INSERTer of this kind):

  first process:
    ↓ build() sees no 'resurrect' inbound → returns TERMINATE_SCRIPT
    llm 1 (TERMINATE_SCRIPT[0]): ava.self.terminate() + tool_call → exec
                                AgentTermination → claim END → process exits
                                → status='terminated'
  test side: POST /api/agents/{id}/messages (chat)
    → deliver_chat_inbound → resurrect_if_terminated: status='terminated'→'idling' + INSERT 'resurrect' inbound
    → session spawn starts fresh process
  post-resurrect process (new PID, same agent_id):
    ↓ build() sees 'resurrect' → returns IDLE_SCRIPT
    ↓ claim processes 'resurrect' inbound, writes marker → idles waiting for next inbound
    (at this stage the fake is not called; teardown terminate API via lifecycle dispatch also
    doesn't call the fake — so IDLE_SCRIPT is never actually consumed, just a defensive "don't
    terminate again")
"""

from __future__ import annotations

import atexit
import time

import psycopg
from langchain_core.messages import AIMessage

import ava
from ops import runner_mode
from shared.config import settings
from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

TERMINATE_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="\u518d\u89c1,\u7b49\u4f1a\u590d\u6d3b\u6211\u3002",
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

IDLE_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content="I processed the wake after resurrection.", usage_metadata=_USAGE),
)


def _is_post_resurrect_process() -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'resurrect' LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build(model: str) -> ScriptedFakeChatModel:
    if _is_post_resurrect_process():
        return ScriptedFakeChatModel(script=IDLE_SCRIPT)
    return ScriptedFakeChatModel(script=TERMINATE_SCRIPT)


def build_waiting_for_chat(model: str) -> ScriptedFakeChatModel:
    """Hold the real old process until its wake has retried, not an arbitrary sleep."""
    if not runner_mode.is_hosted() and not _is_post_resurrect_process():
        agent_id = int(ava.self.AGENT_ID)

        def wait_for_retry() -> None:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with psycopg.connect(settings.data_plane.db_url) as conn:
                    row = conn.execute(
                        "SELECT 1 FROM inbound_messages WHERE agent_id=%s AND kind='chat' "
                        "AND (payload->'resurrection_retry'->>'attempts')::integer>=2 LIMIT 1",
                        (agent_id,),
                    ).fetchone()
                if row is not None:
                    return
                time.sleep(0.05)
            raise RuntimeError("old process never observed its queued wake retry authorization")

        atexit.register(wait_for_retry)
    return build(model)
