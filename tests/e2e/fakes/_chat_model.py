"""ScriptedFakeChatModel -- returns preset AIMessages turn by turn from a SCRIPT list.

The agent process injects it via `AVA_LLM_OVERRIDE=tests.e2e.fakes.scenarios.<name>:build`
env var on startup. The fake does not actually dispatch tool schemas; it only feeds the
next preset response according to the turn cursor.

Why not use LangChain's built-in GenericFakeChatModel: it works via `messages: list[str]`
+ char-by-char emit, with no tool_calls / usage_metadata support, incompatible with the
production path (`AIMessageChunk += chunk` + `message_chunk_to_message` + trailing
`assert usage_metadata is not None`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr


class ScriptExhaustedError(RuntimeError):
    """SCRIPT cursor out of bounds -- scenario did not cover the current turn.

    Dedicated subclass (not IndexError) so e2e fixtures / log scrapers can precisely
    identify this "scenario too short" failure, distinct from other runtime IndexErrors.
    """


class ScriptedFakeChatModel(BaseChatModel):
    """Fake that returns AIMessages turn by turn from a preset script; emits the
    whole message as a single chunk.

    - script: tuple[AIMessage, ...], each a complete response for one LLM turn;
      immutable tuple expresses "scenario is a fixed contract"
    - cursor increments across _astream calls; exceeding len(script) raises
      ScriptExhaustedError
    - bind_tools is no-op (fake does not actually dispatch tool schemas)
    - emits the whole message as a single chunk -- no character-level streaming
    """

    script: tuple[AIMessage, ...]
    _cursor: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedFakeChatModel:
        return self

    @property
    def cursor(self) -> int:
        """Number of turns consumed; fixture / unit test verifies the scenario was fully consumed during teardown."""
        return self._cursor

    def _next_message(self) -> AIMessage:
        if self._cursor >= len(self.script):
            raise ScriptExhaustedError(
                f"ScriptedFakeChatModel: script exhausted at turn {self._cursor} "
                f"(script length {len(self.script)}) -- scenario did not cover turn {self._cursor + 1}"
            )
        msg = self.script[self._cursor]
        self._cursor += 1
        return msg

    def _make_chunk(self, msg: AIMessage) -> ChatGenerationChunk:
        tool_call_chunks: list[ToolCallChunk] = [
            {
                "name": tc["name"],
                "args": json.dumps(tc["args"]),
                "id": tc.get("id"),
                "index": idx,
            }
            for idx, tc in enumerate(msg.tool_calls)
        ]
        # Auto-set default response_metadata aligned with real Anthropic protocol
        # ('tool_use' with non-empty tool_calls, 'end_turn' otherwise). Prevents
        # _validate_stop_reason from mis-firing on e2e fake — scenarios are module-level
        # AIMessage(...) without response_metadata; this layer fills it in. Scenarios
        # that need to explicitly test refusal/max_tokens etc. pass response_metadata=...
        # directly in the AIMessage; when they do, that metadata must include model_provider.
        response_metadata = msg.response_metadata or {  # pyright: ignore[reportUnknownMemberType]
            "model_provider": "anthropic",
            "stop_reason": "tool_use" if msg.tool_calls else "end_turn",
        }
        chunk_msg = AIMessageChunk(
            content=msg.content,  # pyright: ignore[reportUnknownMemberType]
            tool_call_chunks=tool_call_chunks,
            usage_metadata=msg.usage_metadata,
            response_metadata=response_metadata,
        )
        return ChatGenerationChunk(message=chunk_msg)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        msg = self._next_message()
        yield self._make_chunk(msg)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        msg = self._next_message()
        yield self._make_chunk(msg)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._next_message()
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._next_message()
        return ChatResult(generations=[ChatGeneration(message=msg)])
