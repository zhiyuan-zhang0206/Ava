"""Terminate and resurrect the same agent identity through hosted admission.

The initial fake calls ava.self.terminate(). A later chat or explicit resurrect
request creates durable resurrection intent; a newly built hosted runtime sees
that inbound row and selects the follow-up script. The E2E test separately
checks the new generation and delivery of the pending chat after resurrection.
"""

from __future__ import annotations

import psycopg
from langchain_core.messages import AIMessage

import ava
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
    """Use the same hosted resurrection scenario for the queued-wake test."""
    return build(model)
