"""Shared e2e fake: a scripted model that records every call's input.

Real agent turns run against it; tests then assert on `model_inputs()` -- the exact
messages the model was handed -- instead of on a function's return value. Records go to
a JSONL file under `scratch_root(...)`, a temp dir derived from AVA_HOME's name (outside
$HOME, so a developer's own AGENTS.md files never leak into context-file walks). Paths
are functions: the pytest process imports this module before the e2e fixtures set
AVA_HOME, the agent process after; both resolve to the same dir.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def scratch_root(kind: str) -> Path:
    """Per-e2e-session temp dir for one family of scenarios."""
    name = Path(os.environ["AVA_HOME"]).name
    return Path(tempfile.gettempdir()).resolve() / f"ava-e2e-{kind}-{name}"


def record_path() -> Path:
    return scratch_root("record") / "model_inputs.jsonl"


def reset_record() -> None:
    """Forget earlier calls (a test starts from an empty record)."""
    record_path().unlink(missing_ok=True)


def exec_call(n: int, code: str) -> AIMessage:
    """A model turn that asks for one `execute_code`."""
    return AIMessage(
        content="",
        tool_calls=[{"id": f"call_{n}", "name": "execute_code", "args": {"code": code}}],
        usage_metadata=_USAGE,
    )


def say(text: str) -> AIMessage:
    """A model turn that only replies."""
    return AIMessage(content=text, usage_metadata=_USAGE)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    blocks: list[Any] = list(content)
    return "\n".join(
        str(cast(dict[str, Any], b).get("text", "")) if isinstance(b, dict) else str(b)
        for b in blocks
    )


class RecordingModel(ScriptedFakeChatModel):
    """Scripted turns; before each one, append the messages it was handed to the record."""

    agent_id: int | None

    def _record(self, messages: list[BaseMessage]) -> None:
        entry = {
            "agent_id": self.agent_id,
            "pid": os.getpid(),
            "messages": [
                {
                    "type": m.type,
                    "text": _text(m.content),
                    "tag": m.additional_kwargs.get("ava_note_tag"),
                    "tool_calls": [tc["name"] for tc in getattr(m, "tool_calls", [])],
                }
                for m in messages
            ],
        }
        record_path().parent.mkdir(parents=True, exist_ok=True)
        with record_path().open("a") as f:
            f.write(json.dumps(entry) + "\n")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self._record(messages)
        yield self._make_chunk(self._next_message())

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # The compaction summary is a non-streaming call; record it like the rest.
        self._record(messages)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def model_inputs(agent_id: int | None = None) -> list[list[dict[str, Any]]]:
    """Model calls so far (of one agent, or all), oldest first: each call's messages as seen."""
    path = record_path()
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    return [e["messages"] for e in entries if agent_id is None or e["agent_id"] == agent_id]
