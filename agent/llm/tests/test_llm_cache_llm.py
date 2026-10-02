"""The LLM invoke timeout."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.llm.cache import ainvoke_with_cache_retry
from base.agents.context.slices import AgentSlices

_SYSTEM = SystemMessage(content="You are a test agent. " * 100)


_CONVO = [HumanMessage(content="hi"), AIMessage(content="hello")]


class _StubRunnable:
    """Records ainvoke calls; scripts errors."""

    def __init__(self, *, errors: list[Exception] | None = None) -> None:
        self.calls: list[list] = []
        self._errors = list(errors or [])

    async def ainvoke(self, messages: Any) -> AIMessage:
        self.calls.append(list(messages))  # pyright: ignore[reportUnknownMemberType]
        if self._errors:
            raise self._errors.pop(0)
        return AIMessage(content="done")


class _StubLLM:
    """Duck-typed chat model: bind/bind_tools return recording runnables.

    Not a BaseChatModel subclass (pydantic forbids plain attribute assignment);
    call sites cast() it across the typed boundary."""

    def __init__(self, *, invoke_errors: list[Exception] | None = None) -> None:
        self.bind_kwargs: dict | None = None
        self.runnable = _StubRunnable(errors=invoke_errors)

    def bind(self, **kwargs: Any) -> _StubRunnable:
        self.bind_kwargs = kwargs
        return self.runnable

    def bind_tools(self, tools: Any, **kwargs: Any) -> _StubRunnable:
        self.bind_kwargs = {"tools": tools, **kwargs}
        return self.runnable


class TestInvokeTimeout:
    """ainvoke_with_cache_retry is bounded by llm_compact_timeout_seconds — a
    wedged provider must surface as a timeout, not hold the agent's claim
    node at the provider SDK default."""

    async def test_compact_timeout_bounds_the_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _HangingRunnable(_StubRunnable):
            async def ainvoke(self, messages: Any) -> AIMessage:
                self.calls.append(list(messages))  # pyright: ignore[reportUnknownMemberType]
                await asyncio.sleep(30)
                return AIMessage(content="never")

        class _HangingLLM(_StubLLM):
            def __init__(self) -> None:
                super().__init__()
                self.runnable = _HangingRunnable()

        from base.config import settings as _settings

        monkeypatch.setattr(_settings.lm, "llm_compact_timeout_seconds", 0.05)
        llm = _HangingLLM()
        with pytest.raises(TimeoutError):
            await ainvoke_with_cache_retry(
                cast(BaseChatModel, llm), [_SYSTEM, *_CONVO], AgentSlices.resolve().llm_policy
            )

    async def test_fast_call_returns_within_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from base.config import settings as _settings

        monkeypatch.setattr(_settings.lm, "llm_compact_timeout_seconds", 60.0)
        llm = _StubLLM()
        out, used_cache = await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm), [_SYSTEM, *_CONVO], AgentSlices.resolve().llm_policy
        )
        assert out.content == "done"  # pyright: ignore[reportUnknownMemberType]
        assert used_cache is False  # plain path (no cache memo)
