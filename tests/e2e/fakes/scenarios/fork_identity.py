from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk

from base.config import settings
from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

FORK_PROMPT = "fork-redirect: drop the old task and summarize instead"

_MARKER_SUBSTR = "forked from agent:"

FORK_OK = "FORK_CONTEXT_OK"

SOURCE_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content="source agent first turn", usage_metadata=_USAGE),
)


class _ForkContextModel(ScriptedFakeChatModel):
    def _verdict(self, messages: list[BaseMessage]) -> AIMessage:
        blob = "\n".join(str(m.content) for m in messages)
        saw_marker = _MARKER_SUBSTR in blob
        saw_prompt = FORK_PROMPT in blob
        content = (
            FORK_OK
            if (saw_marker and saw_prompt)
            else f"FORK_CONTEXT_MISSING marker={saw_marker} prompt={saw_prompt}"
        )
        return AIMessage(content=content, usage_metadata=_USAGE)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        yield self._make_chunk(self._verdict(messages))


def _has_fork_inbound(agent_id: int | None) -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'fork' LIMIT 1",
            (agent_id,),
        )
        return cur.fetchone() is not None


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    if _has_fork_inbound(agent_id):
        return _ForkContextModel(script=())
    return ScriptedFakeChatModel(script=SOURCE_SCRIPT)
