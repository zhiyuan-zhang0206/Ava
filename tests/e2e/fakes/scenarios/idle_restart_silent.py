"""External restart hitting an idle agent — silent respawn (zero LLM calls) cross-process scenario.

Closed loop (PR #997's NOT-tested boundary):

  first process (before restart):
    ↓ build() sees no 'restart_completed' in DB → returns PRE_RESTART_SCRIPT
    llm 1 (PRE_RESTART_SCRIPT[0]): exec `ava.cwd.set(...)` — writes a non-default
                                   cwd to plugin state as a respawn liveness witness
    llm 2 (PRE_RESTART_SCRIPT[1]): plain-text halt → idle (halted=True checkpoint)
    ↓ test directly POST /api/agents/{id}/restart (source defaults to 'user', external)
    ↓ claim receives 'restart' (idle, external, no chat co-batch) → committed halted=True
    ↓ restarter respawn → INSERT 'restart_completed' + fresh process
  post-restart process (new PID, same agent_id):
    ↓ build() sees 'restart_completed' → returns **empty** script
    ↓ claim receives 'restart_completed' (marker-only batch + halted=True) →
      commits marker then goto CLAIM back to waiting — **no LLM call**
    ↓ if any path accidentally calls the LLM, empty script immediately blows up the
      process with ScriptExhaustedError, status never reaches idling, test times out
      — "zero LLM calls" is a fail-loud assertion

Why cwd as liveness witness: the respawn's ainvoke input is an empty state update ({}),
the plugin state channel (ava_code__cwd) must be read back verbatim from the checkpoint
— the old implementation passed a full AgentState() which would overwrite it back to the
default workspace; this scenario nails down that regression point.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from langchain_core.messages import AIMessage

import ava
from shared.config import settings
from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

# Survival witness written into plugin state before the restart; the test
# asserts the checkpoint still carries it after the silent respawn. Derived
# from the e2e worker's own `$AVA_HOME` (tests/e2e/conftest.py:_e2e_process_env)
# instead of a fixed /tmp path (audit L-1): every e2e session gets its own
# witness dir, and the pre/post-restart processes agree because both inherit
# the same AVA_HOME. Teardown of the e2e home cleans it up.
CWD_WITNESS = str(Path(os.environ["AVA_HOME"]) / "e2e-idle-restart-cwd")

PRE_RESTART_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="\u8bbe\u7f6e\u5de5\u4f5c\u76ee\u5f55\u3002",
        tool_calls=[
            {
                "id": "call_set_cwd",
                "name": "execute_code",
                "args": {
                    "code": (
                        "import ava\nimport os\n"
                        f"os.makedirs({CWD_WITNESS!r}, exist_ok=True)\n"
                        f"ava.cwd.set({CWD_WITNESS!r})"
                    )
                },
            }
        ],
        usage_metadata=_USAGE,
    ),
    AIMessage(content="\u5de5\u4f5c\u76ee\u5f55\u5df2\u8bbe\u7f6e\u3002", usage_metadata=_USAGE),
)

# Post-restart the agent must stay silent: any LLM call exhausts the empty
# script and kills the process loudly (status never reaches idling).
POST_RESTART_SCRIPT: tuple[AIMessage, ...] = ()


def _is_post_restart_process() -> bool:
    """restarter has already INSERTed 'restart_completed' inbound at respawn time = post-restart."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart' AND applied_at IS NOT NULL LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build(model: str) -> ScriptedFakeChatModel:
    if _is_post_restart_process():
        return ScriptedFakeChatModel(script=POST_RESTART_SCRIPT)
    return ScriptedFakeChatModel(script=PRE_RESTART_SCRIPT)
