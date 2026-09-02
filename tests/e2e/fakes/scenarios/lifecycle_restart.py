"""ava.self.restart lifecycle scenario for process and hosted runners.

Build two SCRIPT segments, distinguished by a durable restart record in the
inbound_messages table:

  first process (before restart):
    ↓ build() sees no 'restart_completed' in DB → returns RESTART_SCRIPT
    llm 1 (RESTART_SCRIPT[0]): ava.self.restart() + tool_call → exec raises AgentRestart
                              → claim marks agents.status='restarting' + END → process exits
    ↓ restarter daemon (1s poll) sees status='restarting' → respawn_agent
    ↓   INSERT 'restart_completed' inbound row
    ↓   session spawn starts fresh process
  post-restart runtime (new process in process mode; cold runtime in hosted
  mode, same agent_id):
    ↓ build() sees the consumed 'restart' or 'restart_completed' record → returns IDLE_SCRIPT
    ↓ claim processes 'restart_completed' inbound, writes "[system ts] You have been restarted"
    ↓ marker enters messages → idle, waiting for next inbound (fake is not called at this stage)
    llm 1 (IDLE_SCRIPT[0]): short chat halt, only called if the user sends another message after idle

Why inbound_messages rather than agent events / messages: process mode has the
restarter-written 'restart_completed' marker; hosted mode retains the consumed
'restart' inbound after rendering the marker inline. Both records survive claim
consumption. The truncated_db fixture truncates before each test so the first
runtime sees a DB free of prior test residue.
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


def _is_post_restart_runtime() -> bool:
    """Whether this runtime follows a restart for its agent.

    Process mode: the restarter has INSERTed a 'restart_completed' inbound,
    which only exists after a full restart cycle — the fresh process is
    post-restart. Hosted mode: no restarter exists, so 'restart_completed'
    never appears; the SDK's own 'restart' inbound row marks the first
    restart instead. The fake must switch to the idle script after the first
    restart in both modes, or a hosted agent would keep calling
    ``ava.self.restart()`` forever.
    """
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'restart' LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        if cur.fetchone() is not None:
            return True
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart_completed' LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build(model: str) -> ScriptedFakeChatModel:
    if _is_post_restart_runtime():
        return ScriptedFakeChatModel(script=IDLE_SCRIPT)
    return ScriptedFakeChatModel(script=RESTART_SCRIPT)
