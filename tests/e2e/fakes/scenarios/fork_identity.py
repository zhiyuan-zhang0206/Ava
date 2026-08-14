"""Fork identity + prompt — cross-process e2e scenario.

A fork copies the source agent's full history into a new agent. The new
process must learn, before its first turn, that it is a new identity (the
`kind='fork'` lifecycle marker) and must see the prompt the fork carried — both
delivered before launch so they land in its first claim batch. This scenario
proves that end-to-end on a real forked subprocess.

Two branches, distinguished by whether THIS process's agent has a `kind='fork'`
inbound (only a forked agent does — committed before its launch):

  source process (no fork inbound):
    runs one scripted turn so it produces a checkpoint for the fork to copy.

  forked process (has fork inbound):
    instead of a fixed script, `_ForkContextModel` inspects the messages it
    actually receives on its first turn and replies with a sentinel encoding
    whether BOTH the fork identity marker and the prompt were present in
    context — turning "did the forked agent see them" into an assertable reply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk

import ava
from shared.config import settings
from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

# The prompt the test forks with — distinctive so the forked fake can spot it in
# its received context, and the test can spot its inbound_chat timeline item.
FORK_PROMPT = "fork-redirect: drop the old task and summarize instead"

# Substring the claim node writes into the fork identity marker (see
# agent/graph/_claim.py FORK case): "...You have been forked from agent:M...".
_MARKER_SUBSTR = "forked from agent:"

# Sentinel the forked agent replies with iff both marker and prompt were in its
# first-turn context. The test asserts this exact reply lands in the timeline.
FORK_OK = "FORK_CONTEXT_OK"

# One trivial turn for the source so it commits a checkpoint the fork can copy.
SOURCE_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(content="source agent first turn", usage_metadata=_USAGE),
)


class _ForkContextModel(ScriptedFakeChatModel):
    """Forked-process fake: replies with a verdict computed from the messages it
    is actually given, not a fixed script. Reaching the agent's timeline with
    `FORK_OK` proves the forked model's first-turn input held both the fork
    identity marker and the prompt."""

    def _verdict(self, messages: list[BaseMessage]) -> AIMessage:
        # str() on a str returns it unchanged and on a list renders its repr —
        # the original conditional's exact semantics, in one typed line
        # (langchain's BaseMessage.content has Unknown slots in its stub).
        blob = "\n".join(
            str(m.content)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            for m in messages
        )
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
        # The agent's llm node consumes `astream` only (agent/graph/_llm.py); the
        # sync `_stream` is never reached, so it keeps the base script behavior.
        yield self._make_chunk(self._verdict(messages))


def _has_fork_inbound() -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'fork' LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build(model: str) -> ScriptedFakeChatModel:
    if _has_fork_inbound():
        return _ForkContextModel(script=())
    return ScriptedFakeChatModel(script=SOURCE_SCRIPT)
