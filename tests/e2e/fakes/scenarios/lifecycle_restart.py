"""ava.self.restart — agent self-restart + restarter spawns new process lifecycle scenario.

Cross-process script selection uses a persisted consumed self-restart request.
It is deliberately independent from successful successor completion evidence.

  first process (before restart):
    ↓ build() sees no consumed self-restart request → returns RESTART_SCRIPT
    llm 1 (RESTART_SCRIPT[0]): ava.self.restart() + tool_call → exec raises AgentRestart
                              → claim marks agents.status='restarting' + END → process exits
    ↓ restarter daemon (1s poll) sees status='restarting' → respawn_agent
    ↓   INSERT 'restart_completed' inbound row
    ↓   session spawn starts fresh process
  post-restart process (new PID, same agent_id):
    ↓ build() sees the prior claimed/done self-restart request → returns IDLE_SCRIPT
    ↓ claim processes 'restart_completed' inbound, writes "[system ts] You have been restarted"
    ↓ marker enters messages → idle, waiting for next inbound (fake is not called at this stage)
    llm 1 (IDLE_SCRIPT[0]): short chat halt, only called if the user sends another message after idle

Using a missing completion marker to select the script made a fresh fake model
request another restart after the original request was already consumed,
obscuring the actual missing-completion defect. The E2E test separately requires
durable-restarter completion evidence; this selector neither writes nor
fabricates it.
The fixture truncates before each test. A pending request is not consumed and
must not select the post-request script.
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
