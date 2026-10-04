"""ava.context in a real exec child — identity from the host, a DB call and a gateway call.

Script:
  turn 1: execute_code reads `ava.context.identity` (the identity the agent host put in the
          exec request envelope), then uses it for one SQL read over `ava.context.sql` and
          two gateway reads (`ava.agents.get_status`, `ava.context.gateway`), and prints what it saw
  turn 2: final reply text

The e2e agent runs its real graph and a real exec child, so the printed line proves the child
rebuilt the host's context and that the identity it carries addresses the same agent in the
database and at the gateway.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

REPLY_TEXT = "context ok"

CODE = """\
import ava

who = ava.context.identity
with ava.context.sql.cursor() as cur:
    cur.execute("SELECT id FROM agents_meta WHERE id = %s", (who.agent_id,))
    row = cur.fetchone()
status = ava.agents.get_status(who.agent_id)
reply = ava.context.gateway.get(f"/api/agents/{who.agent_id}")
print(f"CTX agent_id={who.agent_id} owns_loop={who.owns_loop} actor={who.actor} db_row={row[0]} status={status.value} gateway={reply.status_code}")
"""

SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="reading my context",
        tool_calls=[{"id": "call_ctx", "name": "execute_code", "args": {"code": CODE}}],
        usage_metadata=_USAGE,
    ),
    AIMessage(content=REPLY_TEXT, usage_metadata=_USAGE),
)


def build(model: str) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=SCRIPT)
