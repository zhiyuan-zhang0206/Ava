"""Tests for agent/lm_cache.py — invocation prep + stale-cache retry glue."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.lm_cache import ainvoke_with_cache_retry, prepare_invocation
from ava_builtins.plugins.lm_google import gemini_cache
from ava_builtins.plugins.lm_google.gemini_cache import CacheRef

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


def _ref() -> CacheRef:
    return CacheRef(
        name="cachedContents/t1",
        key="k1",
        expire_time=datetime.now(UTC) + timedelta(hours=1),
    )


class TestPrepareInvocation:
    async def test_cache_path_strips_system_and_binds_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _StubLLM()
        ref = _ref()
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(ref),  # pyright: ignore[reportUnknownArgumentType]
        )
        inv = await prepare_invocation(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert inv.cache_ref is ref
        assert inv.messages == _CONVO  # SystemMessage stripped
        assert llm.bind_kwargs == {"cached_content": "cachedContents/t1"}  # pyright: ignore[reportUnknownMemberType]

    async def test_plain_path_keeps_system_and_binds_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _StubLLM()
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(None),  # pyright: ignore[reportUnknownArgumentType]
        )
        inv = await prepare_invocation(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert inv.cache_ref is None
        assert inv.messages == [_SYSTEM, *_CONVO]
        assert "tools" in (llm.bind_kwargs or {})  # pyright: ignore[reportUnknownMemberType]

    async def test_cache_not_attempted_without_leading_system(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def _spy(*a: Any, **k: Any) -> Any:
            nonlocal called
            called = True
            return _async_return(None)

        monkeypatch.setattr(gemini_cache, "get_or_create_cache", _spy)
        inv = await prepare_invocation(cast(BaseChatModel, _StubLLM()), list(_CONVO))
        assert inv.cache_ref is None and not called

    async def test_cache_not_attempted_with_second_system_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second SystemMessage would merge into the request's
        system_instruction (langchain _parse_chat_history) and 400 against the
        cache — plain path instead."""
        called = False

        def _spy(*a: Any, **k: Any) -> Any:
            nonlocal called
            called = True
            return _async_return(None)

        monkeypatch.setattr(gemini_cache, "get_or_create_cache", _spy)
        inv = await prepare_invocation(
            cast(BaseChatModel, _StubLLM()),
            [_SYSTEM, HumanMessage(content="x"), SystemMessage(content="later")],
        )
        assert inv.cache_ref is None and not called


class TestAinvokeWithCacheRetry:
    async def test_success_no_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _StubLLM()
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(_ref()),  # pyright: ignore[reportUnknownArgumentType]
        )
        response = await ainvoke_with_cache_retry(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert response.content == "done"  # pyright: ignore[reportUnknownMemberType]
        assert len(llm.runnable.calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    async def test_stale_cache_retries_plain_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from google.genai.errors import ClientError

        stale = ClientError(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "CachedContent not found (or permission denied)",
                    "status": "PERMISSION_DENIED",
                }
            },
        )
        llm = _StubLLM(invoke_errors=[stale])
        invalidated: list[str] = []
        # Production: invalidate() drops the memo, so the retry's prepare takes
        # the plain path. Mirror that by consulting the invalidated list.
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(None if invalidated else _ref()),  # pyright: ignore[reportUnknownArgumentType]
        )
        monkeypatch.setattr(gemini_cache, "invalidate", lambda ref: invalidated.append(ref.name))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        response = await ainvoke_with_cache_retry(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert response.content == "done"  # pyright: ignore[reportUnknownMemberType]
        assert invalidated == ["cachedContents/t1"]
        # first call stripped (cache path), retry full (plain path)
        assert llm.runnable.calls[0] == _CONVO  # pyright: ignore[reportUnknownMemberType]
        assert llm.runnable.calls[1] == [_SYSTEM, *_CONVO]  # pyright: ignore[reportUnknownMemberType]

    async def test_non_stale_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _StubLLM(invoke_errors=[ValueError("boom")])
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(_ref()),  # pyright: ignore[reportUnknownArgumentType]
        )
        with pytest.raises(ValueError, match="boom"):
            await ainvoke_with_cache_retry(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert len(llm.runnable.calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    async def test_plain_path_error_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from google.genai.errors import ClientError

        stale = ClientError(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "CachedContent not found (or permission denied)",
                    "status": "PERMISSION_DENIED",
                }
            },
        )
        llm = _StubLLM(invoke_errors=[stale])
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(None),  # pyright: ignore[reportUnknownArgumentType]
        )
        with pytest.raises(ClientError):
            await ainvoke_with_cache_retry(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert len(llm.runnable.calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


async def _async_return(value: Any) -> Any:
    return value


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

        from shared.config import settings as _settings

        monkeypatch.setattr(_settings.lm, "llm_compact_timeout_seconds", 0.05)
        llm = _HangingLLM()
        with pytest.raises(TimeoutError):
            await ainvoke_with_cache_retry(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])

    async def test_fast_call_returns_within_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.config import settings as _settings

        monkeypatch.setattr(_settings.lm, "llm_compact_timeout_seconds", 60.0)
        llm = _StubLLM()
        out = await ainvoke_with_cache_retry(cast(BaseChatModel, llm), [_SYSTEM, *_CONVO])
        assert out.content == "done"  # pyright: ignore[reportUnknownMemberType]
