"""An external restart preserves the idle checkpoint without a model call.

The first hosted incarnation writes a non-default plugin cwd and halts. After
an ordinary external restart is applied, a newly built fake selects an empty
script. Any accidental model call then raises ScriptExhaustedError. The test
separately checks the durable restart marker, unchanged message count and cwd.

The cwd witnesses cold checkpoint restoration: an empty state update must
preserve the existing plugin channel rather than replace it with a default.
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
# script and fails the invocation loudly.
POST_RESTART_SCRIPT: tuple[AIMessage, ...] = ()


def _is_post_restart_process() -> bool:
    """A durable restart was applied before constructing this hosted fake."""
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
