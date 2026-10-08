"""The LLM invoke timeout."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.llm.cache import ainvoke_with_cache_retry, prepare_invocation
from base.host.env.agent_slices import AgentSlices
from base.lm.call import LlmInvocation, ProviderCallContext
from base.lm.provider_api import ProviderBinding

_SYSTEM = SystemMessage(content="You are a test agent. " * 100)


_CONVO = [HumanMessage(content="hi"), AIMessage(content="hello")]


class _StubRunnable:
    """Records ainvoke calls; scripts errors."""

    def __init__(self, *, errors: list[Exception] | None = None) -> None:
        self.calls: list[list[Any]] = []
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


@pytest.mark.parametrize("nested", [True, False])
async def test_recovery_rejects_a_cached_or_nested_retry(nested: bool) -> None:
    llm = _StubLLM(invoke_errors=[ValueError("first failed")])

    def recover(_exc: BaseException) -> LlmInvocation:
        return LlmInvocation(
            llm.runnable, [], used_explicit_cache=not nested, recover=recover if nested else None
        )

    async def prepare(ctx: ProviderCallContext) -> LlmInvocation:
        return LlmInvocation(llm.runnable, ctx.messages, recover=recover)

    def build(_ctx: object) -> BaseChatModel:
        return cast(BaseChatModel, llm)

    binding = ProviderBinding("test-", "Test", "TEST_KEY", build, prepare_call=prepare)
    with pytest.raises(ValueError, match="plain invocation"):
        await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            list(_CONVO),
            AgentSlices.resolve().llm_policy,
            binding=binding,
        )
    assert len(llm.runnable.calls) == 1


async def test_prepare_is_inside_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.config import settings

    preparing = asyncio.Event()
    cancelled = asyncio.Event()

    async def prepare(_ctx: ProviderCallContext) -> LlmInvocation | None:
        preparing.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    llm = _StubLLM()

    def build(_ctx: object) -> BaseChatModel:
        return cast(BaseChatModel, llm)

    binding = ProviderBinding("test-", "Test", "TEST_KEY", build, prepare_call=prepare)
    monkeypatch.setattr(settings.lm, "llm_compact_timeout_seconds", 0.05)
    with pytest.raises(TimeoutError):
        await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            list(_CONVO),
            AgentSlices.resolve().llm_policy,
            binding=binding,
        )
    assert preparing.is_set() and cancelled.is_set()
    assert llm.runnable.calls == []


async def test_malformed_provider_runnable_fails_at_preparation() -> None:
    llm = _StubLLM()

    async def prepare(ctx: ProviderCallContext) -> LlmInvocation:
        return LlmInvocation(object(), ctx.messages)

    def build(_ctx: object) -> BaseChatModel:
        return cast(BaseChatModel, llm)

    binding = ProviderBinding("test-", "Test", "TEST_KEY", build, prepare_call=prepare)
    with pytest.raises(TypeError, match="support ainvoke"):
        await prepare_invocation(
            cast(BaseChatModel, llm), list(_CONVO), AgentSlices.resolve().llm_policy, binding
        )
    assert llm.runnable.calls == []


async def test_wire_cancellation_never_calls_recovery() -> None:
    interruption = asyncio.CancelledError()
    recovered: list[BaseException] = []

    class CancelledRunnable:
        async def ainvoke(self, _messages: object) -> AIMessage:
            raise interruption

    def recover(exc: BaseException) -> LlmInvocation | None:
        recovered.append(exc)
        return None

    async def prepare(ctx: ProviderCallContext) -> LlmInvocation:
        return LlmInvocation(CancelledRunnable(), ctx.messages, recover=recover)

    llm = _StubLLM()

    def build(_ctx: object) -> BaseChatModel:
        return cast(BaseChatModel, llm)

    binding = ProviderBinding("test-", "Test", "TEST_KEY", build, prepare_call=prepare)
    with pytest.raises(asyncio.CancelledError):
        await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            list(_CONVO),
            AgentSlices.resolve().llm_policy,
            binding=binding,
        )
    assert recovered == []
