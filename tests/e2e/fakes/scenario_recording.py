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

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel

__all__ = ["RecordingModel", "exec_call", "model_inputs", "reset_record", "say", "scratch_root"]

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def scratch_root(kind: str) -> Path:
    name = Path(os.environ["MEIVA_HOME"]).name
    return Path(tempfile.gettempdir()).resolve() / f"meiva-e2e-{kind}-{name}"


def _record_path() -> Path:
    return scratch_root("record") / "model_inputs.jsonl"


def reset_record() -> None:
    _record_path().unlink(missing_ok=True)


def exec_call(n: int, code: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"call_{n}", "name": "execute_code", "args": {"code": code}}],
        usage_metadata=_USAGE,
    )


def say(text: str) -> AIMessage:
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
    agent_id: int | None

    def _record(self, messages: list[BaseMessage]) -> None:
        entry = {
            "agent_id": self.agent_id,
            "pid": os.getpid(),
            "messages": [
                {
                    "type": m.type,
                    "text": _text(m.content),
                    "tag": m.additional_kwargs.get("meiva_note_tag"),
                    "tool_calls": [tc["name"] for tc in getattr(m, "tool_calls", [])],
                }
                for m in messages
            ],
        }
        _record_path().parent.mkdir(parents=True, exist_ok=True)
        with _record_path().open("a") as f:
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
        self._record(messages)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def _complete_lines_text(path: Path) -> str:
    complete, newline, _partial = path.read_text().rpartition("\n")
    return complete + newline


def model_inputs(agent_id: int | None = None) -> list[list[dict[str, Any]]]:
    path = _record_path()
    if not path.exists():
        return []
    entries = [json.loads(line) for line in _complete_lines_text(path).splitlines()]
    return [e["messages"] for e in entries if agent_id is None or e["agent_id"] == agent_id]
