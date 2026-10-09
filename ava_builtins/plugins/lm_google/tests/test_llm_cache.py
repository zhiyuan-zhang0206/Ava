"""Tests for agent/llm/cache.py — invocation prep + stale-cache retry glue."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from google.genai.errors import ClientError
from langchain_core.exceptions import ModelPermissionDeniedError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai.chat_models import _handle_client_error

from agent.llm.cache import ainvoke_with_cache_retry, prepare_invocation
from ava_builtins.plugins.lm_google import gemini_cache
from ava_builtins.plugins.lm_google.gemini_cache import CacheRef
from ava_builtins.plugins.lm_google.provider import PROVIDER
from base.host.env.agent_slices import AgentSlices
from base.lm.call import recover_invocation
from base.lm.errors import ErrorClass, classify_error

_SYSTEM = SystemMessage(content="You are a test agent. " * 100)
_CONVO = [HumanMessage(content="hi"), AIMessage(content="hello")]


class _ProgrammingError(TypeError):
    code = 403


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
        inv = await prepare_invocation(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert inv.used_explicit_cache and inv.recover is not None
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
        inv = await prepare_invocation(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert not inv.used_explicit_cache
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
        inv = await prepare_invocation(
            cast(BaseChatModel, _StubLLM()),
            list(_CONVO),
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert not inv.used_explicit_cache and not called

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
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert not inv.used_explicit_cache and not called


class TestAinvokeWithCacheRetry:
    async def test_success_no_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _StubLLM()
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(_ref()),  # pyright: ignore[reportUnknownArgumentType]
        )
        response, used_cache = await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert response.content == "done"  # pyright: ignore[reportUnknownMemberType]
        assert used_cache is True  # cache-bound attempt succeeded
        assert len(llm.runnable.calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    @pytest.mark.parametrize("official_wrapper", [False, True])
    async def test_stale_cache_retries_plain_once(
        self, monkeypatch: pytest.MonkeyPatch, official_wrapper: bool
    ) -> None:
        stale: Exception = ClientError(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "CachedContent not found (or permission denied)",
                    "status": "PERMISSION_DENIED",
                }
            },
        )
        if official_wrapper:
            try:
                _handle_client_error(stale, {"model": "gemini-3.7-flash"})
            except ModelPermissionDeniedError as wrapped:
                stale = wrapped
        llm = _StubLLM(invoke_errors=[stale])
        invalidated: list[str] = []
        # Even a still-eligible cache cannot be prepared again on recovery.
        prepared: list[object] = []

        async def prepare(*args: object) -> CacheRef:
            prepared.append(args)
            return _ref()

        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            prepare,
        )
        monkeypatch.setattr(gemini_cache, "invalidate", lambda ref: invalidated.append(ref.name))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        response, used_cache = await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert response.content == "done"  # pyright: ignore[reportUnknownMemberType]
        assert used_cache is False  # stale-cache retry landed on the plain path
        assert invalidated == ["cachedContents/t1"]
        assert len(prepared) == 1
        # first call stripped (cache path), retry full (plain path)
        assert llm.runnable.calls[0] == _CONVO  # pyright: ignore[reportUnknownMemberType]
        assert llm.runnable.calls[1] == [_SYSTEM, *_CONVO]  # pyright: ignore[reportUnknownMemberType]

    @pytest.mark.parametrize("provider_cause", [False, True])
    async def test_programming_error_cannot_authorize_plain_cache_retry(
        self, monkeypatch: pytest.MonkeyPatch, provider_cause: bool
    ) -> None:
        error = _ProgrammingError("CachedContent not found (application metadata bug)")
        if provider_cause:
            error.__cause__ = ClientError(
                403, {"error": {"message": "CachedContent not found", "code": 403}}
            )
        llm = _StubLLM(invoke_errors=[error])
        invalidated: list[str] = []

        async def prepare(*args: object) -> CacheRef:
            return _ref()

        def invalidate(ref: CacheRef) -> None:
            invalidated.append(ref.name)

        monkeypatch.setattr(gemini_cache, "get_or_create_cache", prepare)
        monkeypatch.setattr(gemini_cache, "invalidate", invalidate)
        invocation = await prepare_invocation(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
        assert invocation.used_explicit_cache
        assert classify_error(error).error_class is ErrorClass.UNKNOWN
        assert recover_invocation(invocation, error) is None
        with pytest.raises(TypeError) as raised:
            await ainvoke_with_cache_retry(
                cast(BaseChatModel, llm),
                [_SYSTEM, *_CONVO],
                AgentSlices.resolve().llm_policy,
                binding=PROVIDER.binding,
            )
        assert raised.value is error
        assert len(llm.runnable.calls) == 1
        assert invalidated == []

    async def test_non_stale_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _StubLLM(invoke_errors=[ValueError("boom")])
        monkeypatch.setattr(
            gemini_cache,
            "get_or_create_cache",
            lambda *_a, **_k: _async_return(_ref()),  # pyright: ignore[reportUnknownArgumentType]
        )
        with pytest.raises(ValueError, match="boom"):
            await ainvoke_with_cache_retry(
                cast(BaseChatModel, llm),
                [_SYSTEM, *_CONVO],
                AgentSlices.resolve().llm_policy,
                binding=PROVIDER.binding,
            )
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
            await ainvoke_with_cache_retry(
                cast(BaseChatModel, llm),
                [_SYSTEM, *_CONVO],
                AgentSlices.resolve().llm_policy,
                binding=PROVIDER.binding,
            )
        assert len(llm.runnable.calls) == 1  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


async def _async_return(value: Any) -> Any:
    return value


@pytest.mark.parametrize(
    "interruption", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()]
)
async def test_recovery_preserves_base_exceptions(
    monkeypatch: pytest.MonkeyPatch, interruption: BaseException
) -> None:
    monkeypatch.setattr(gemini_cache, "get_or_create_cache", _ready_cache)
    classified: list[BaseException] = []

    def classifier(exc: BaseException) -> bool:
        classified.append(exc)
        return True

    monkeypatch.setattr(gemini_cache, "is_stale_cache_error", classifier)
    invocation = await prepare_invocation(
        cast(BaseChatModel, _StubLLM()),
        [_SYSTEM, *_CONVO],
        AgentSlices.resolve().llm_policy,
        PROVIDER.binding,
    )
    assert invocation.recover is not None
    with pytest.raises(type(interruption)) as raised:
        invocation.recover(interruption)
    assert raised.value is interruption
    assert classified == []


async def test_single_attempt_never_invokes_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ValueError("single wire failure")
    llm = _StubLLM(invoke_errors=[error])
    monkeypatch.setattr(gemini_cache, "get_or_create_cache", _ready_cache)
    classified: list[BaseException] = []

    def classifier(exc: BaseException) -> bool:
        classified.append(exc)
        return True

    monkeypatch.setattr(gemini_cache, "is_stale_cache_error", classifier)
    with pytest.raises(ValueError, match="single wire failure"):
        await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
            retry_stale_cache=False,
        )
    assert len(llm.runnable.calls) == 1
    assert classified == []


async def test_plain_recovery_wire_failure_has_no_third_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _StubLLM(invoke_errors=[ValueError("stale"), RuntimeError("plain failed")])
    monkeypatch.setattr(gemini_cache, "get_or_create_cache", _ready_cache)
    monkeypatch.setattr(gemini_cache, "is_stale_cache_error", _classify_stale)
    with pytest.raises(RuntimeError, match="plain failed"):
        await ainvoke_with_cache_retry(
            cast(BaseChatModel, llm),
            [_SYSTEM, *_CONVO],
            AgentSlices.resolve().llm_policy,
            binding=PROVIDER.binding,
        )
    assert len(llm.runnable.calls) == 2


async def _ready_cache(*_args: object) -> CacheRef:
    return _ref()


def _classify_stale(_exc: BaseException) -> bool:
    return True
