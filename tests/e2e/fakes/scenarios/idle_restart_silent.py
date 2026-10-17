from __future__ import annotations

import os
from pathlib import Path

import psycopg
from langchain_core.messages import AIMessage

from base.config import settings
from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

CWD_WITNESS = str(Path(os.environ["MEIVA_HOME"]) / "e2e-idle-restart-cwd")

PRE_RESTART_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content="\u8bbe\u7f6e\u5de5\u4f5c\u76ee\u5f55\u3002",
        tool_calls=[
            {
                "id": "call_set_cwd",
                "name": "execute_code",
                "args": {
                    "code": (
                        "import meiva\nimport os\n"
                        f"os.makedirs({CWD_WITNESS!r}, exist_ok=True)\n"
                        f"meiva.cwd.set({CWD_WITNESS!r})"
                    )
                },
            }
        ],
        usage_metadata=_USAGE,
    ),
    AIMessage(content="\u5de5\u4f5c\u76ee\u5f55\u5df2\u8bbe\u7f6e\u3002", usage_metadata=_USAGE),
)

POST_RESTART_SCRIPT: tuple[AIMessage, ...] = ()


def _is_post_restart_process(agent_id: int | None) -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart' AND applied_at IS NOT NULL LIMIT 1",
            (agent_id,),
        )
        return cur.fetchone() is not None


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    if _is_post_restart_process(agent_id):
        return ScriptedFakeChatModel(script=POST_RESTART_SCRIPT)
    return ScriptedFakeChatModel(script=PRE_RESTART_SCRIPT)
