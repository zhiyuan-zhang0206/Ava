"""ava.self.restart — agent self-restart + restarter spawns new process lifecycle scenario.

Cross-process build of two SCRIPT segments, distinguished by the presence of a
kind='restart_completed' row in the inbound_messages table:

  first process (before restart):
    ↓ build() sees no 'restart_completed' in DB → returns RESTART_SCRIPT
    llm 1 (RESTART_SCRIPT[0]): ava.self.restart() + tool_call → exec raises AgentRestart
                              → claim marks agents.status='restarting' + END → process exits
    ↓ restarter daemon (1s poll) sees status='restarting' → respawn_agent
    ↓   INSERT 'restart_completed' inbound row
    ↓   session spawn starts fresh process
  post-restart process (new PID, same agent_id):
    ↓ build() sees 'restart_completed' in DB (restarter just INSERTed) → returns IDLE_SCRIPT
    ↓ claim processes 'restart_completed' inbound, writes "[system ts] You have been restarted"
    ↓ marker enters messages → idle, waiting for next inbound (fake is not called at this stage)
    llm 1 (IDLE_SCRIPT[0]): short chat halt, only called if the user sends another message after idle

Why inbound_messages rather than agent_events / messages: the 'restart_completed' inbound
is the only marker row the restarter definitively writes on respawn, with a precise kind
field; even after claim consumes it, the row is not deleted. The truncated_db fixture
TRUNCATEs before each test so the first process sees a DB free of prior test residue.
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
    AIMessage(content="\u6211\u5df2\u91cd\u542f\u5b8c\u6210\u3002", usage_metadata=_USAGE),
)


def _is_post_restart_process() -> bool:
    """Fresh process starts, restarter has already INSERTed 'restart_completed' inbound = post-restart."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart_completed' LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build(model: str) -> ScriptedFakeChatModel:
    if _is_post_restart_process():
        return ScriptedFakeChatModel(script=IDLE_SCRIPT)
    return ScriptedFakeChatModel(script=RESTART_SCRIPT)
