"""Self restart selects a fresh hosted fake from the consumed request.

The initial fake requests ava.self.restart(). Once that request is claimed or
done, later runtime construction selects a follow-up script. Selection is
independent of successful successor completion: the E2E test separately checks
the applied/observed lifecycle pointer, new generation and a real follow-up.
A pending request must not select the successor script.
"""

from __future__ import annotations

import psycopg
from langchain_core.messages import AIMessage

import ava
from shared.config import settings
from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

RESTART_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="\u91cd\u542f\u81ea\u5df1\u3002",
        tool_calls=[
            {
                "id": "call_restart",
                "name": "execute_code",
                "args": {"code": "import ava\nava.self.restart()"},
            }
        ],
        usage_metadata=_USAGE,
    ),
)

IDLE_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content="Follow-up processed by successor.", usage_metadata=_USAGE),
    AIMessage(content="Follow-up processed by successor.", usage_metadata=_USAGE),
)


def _has_consumed_self_restart() -> bool:
    """A prior self request was consumed, not a claim that restart succeeded."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart' AND source = 'self' "
            "AND status IN ('claimed','done') LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build(model: str) -> ScriptedFakeChatModel:
    if _has_consumed_self_restart():
        return ScriptedFakeChatModel(script=IDLE_SCRIPT)
    return ScriptedFakeChatModel(script=RESTART_SCRIPT)
