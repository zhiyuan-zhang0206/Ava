"""Tests for the lm_google plugin's explicit cachedContents lifecycle.

No API hits: the genai client's async caches surface is faked; the llm is a
real ChatGoogleGenerativeAI (construction is offline) with its client swapped
for the fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiohttp import ServerDisconnectedError, ServerTimeoutError
from google.genai.errors import ClientError, ServerError
from httpx import ConnectError, ReadTimeout
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from ava_builtins.plugins.lm_google import gemini_cache
from ava_builtins.plugins.lm_google.gemini_cache import (
    CacheRef,
    get_or_create_cache,
    invalidate,
    is_stale_cache_error,
)
from base.config import settings
from base.host.env.agent_slices import AgentSlices


@tool("execute_code", parse_docstring=True)
def _fake_tool(code: str) -> str:
    """Run code.

    Args:
        code: source.
    """
    raise NotImplementedError


_BIG_PROMPT = "You are a test agent. " * 2000  # ~10k chars -> est ~2.5k tokens, above guard
_FAR_FUTURE = datetime.now(UTC) + timedelta(seconds=3600)


@pytest.fixture(autouse=True)
def _explicit_cache_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag default flipped to False (task #2660); this module exercises
    the explicit-cache path, so pin it on per test — the off path has its own
    test (`test_flag_off_returns_none`)."""
    monkeypatch.setattr(settings.lm, "gemini_explicit_cache_enabled", True)


class _FakeAsyncPager:
    def __init__(self, items: list[Any], error: Exception | None = None) -> None:
        self._items = items
        self._error = error

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[Any]:
        for item in self._items:
            yield item
        if self._error is not None:
            raise self._error


class _FakeCaches:
    """Fake `client.aio.caches` — records calls, scripts failures."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.update_calls: list[str] = []
        self.list_calls = 0
        self.list_items: list[Any] = []
        self.create_error: Exception | None = None
        self.list_error: Exception | None = None
        self.page_error: Exception | None = None
        self.update_error: Exception | None = None
        self.created_expire = datetime.now(UTC) + timedelta(seconds=3600)

    async def create(self, *, model: str, config: Any) -> Any:
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        from google.genai import types

        return types.CachedContent(
            name=f"cachedContents/fake{self.create_calls}",
            expire_time=self.created_expire,
            usage_metadata=types.CachedContentUsageMetadata(total_token_count=9000),
        )

    async def list(self, *, config: Any = None) -> Any:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return _FakeAsyncPager(self.list_items, self.page_error)

    async def update(self, *, name: str, config: Any) -> Any:
        self.update_calls.append(name)
        if self.update_error is not None:
            raise self.update_error
        from google.genai import types

        return types.CachedContent(
            name=name,
            expire_time=datetime.now(UTC) + timedelta(seconds=3600),
        )


class _HangingCaches(_FakeCaches):
    """Fake whose create/list/update hang until cancelled — simulates a wedged
    Gemini API, which the google-genai SDK would otherwise wait on forever.

    An expired owned request deadline permits the declared cache-only recovery.
    External cancellation and unrelated errors must still propagate.
    """

    def __init__(
        self,
        *,
        hang_create: bool = False,
        hang_list: bool = False,
        hang_update: bool = False,
    ) -> None:
        super().__init__()
        self._hang_create = hang_create
        self._hang_list = hang_list
        self._hang_update = hang_update
        self.create_attempts = 0
        self.list_attempts = 0
        self.update_attempts = 0

    @staticmethod
    async def _hang() -> None:
        await asyncio.Event().wait()

    async def create(self, *, model: str, config: Any) -> Any:
        self.create_attempts += 1
        if self._hang_create:
            await self._hang()
        return await super().create(model=model, config=config)

    async def list(self, *, config: Any = None) -> Any:
        self.list_attempts += 1
        if self._hang_list:
            await self._hang()
        return await super().list(config=config)

    async def update(self, *, name: str, config: Any) -> Any:
        self.update_attempts += 1
        if self._hang_update:
            await self._hang()
        return await super().update(name=name, config=config)


class _FakeAio:
    def __init__(self, caches: _FakeCaches) -> None:
        self.caches = caches


class _FakeClient:
    def __init__(self, caches: _FakeCaches) -> None:
        self.aio = _FakeAio(caches)


class _NonGeminiChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake-non-gemini"

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def _gemini_llm(caches: _FakeCaches) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(model="gemini-3.8-flash", google_api_key="fake-key")
    object.__setattr__(llm, "client", _FakeClient(caches))
    return llm


@pytest.fixture(autouse=True)
def _clear_memo():
    gemini_cache._MEMO.clear()
    gemini_cache._NEGATIVE.clear()
    yield
    gemini_cache._MEMO.clear()
    gemini_cache._NEGATIVE.clear()


class TestGetOrCreate:
    async def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "gemini_explicit_cache_enabled", False)
        caches = _FakeCaches()
        assert (
            await get_or_create_cache(
                _gemini_llm(caches), _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )
        assert caches.create_calls == 0

    async def test_non_gemini_returns_none(self) -> None:
        assert (
            await get_or_create_cache(
                _NonGeminiChatModel(), _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )

    async def test_short_prompt_skipped(self) -> None:
        caches = _FakeCaches()
        assert (
            await get_or_create_cache(
                _gemini_llm(caches), "tiny prompt", [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )
        assert caches.create_calls == 0

    async def test_create_success_and_memo_hit(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None
        assert ref.name == "cachedContents/fake1"
        assert caches.create_calls == 1
        # second call: memo hit, no new create
        ref2 = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref2 is not None and ref2.name == ref.name
        assert caches.create_calls == 1

    async def test_create_failure_negative_memo(self) -> None:
        caches = _FakeCaches()
        caches.create_error = ClientError(429, {"error": {"code": 429, "message": "quota"}})
        llm = _gemini_llm(caches)
        assert (
            await get_or_create_cache(
                llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )
        assert caches.create_calls == 1
        # within the negative window no retry hits the wire
        assert (
            await get_or_create_cache(
                llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )
        assert caches.create_calls == 1

    async def test_different_prompt_gets_own_cache(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref_a = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        ref_b = await get_or_create_cache(
            llm, _BIG_PROMPT + "extra", [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref_a is not None and ref_b is not None
        assert ref_a.name != ref_b.name
        assert caches.create_calls == 2

    async def test_adopt_existing_live_cache(self) -> None:
        from google.genai import types

        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        # pre-seed the "other process" cache with the display_name convention
        from langchain_google_genai._function_utils import (
            convert_to_genai_function_declarations,
        )

        from ava_builtins.plugins.lm_google.gemini_cache import _hash_material

        key = _hash_material(
            llm.model, _BIG_PROMPT, convert_to_genai_function_declarations([_fake_tool])
        )
        caches.list_items = [
            types.CachedContent(
                name="cachedContents/frompeer",
                display_name=f"ava-sys-{llm.model}-{key[:16]}",
                expire_time=datetime.now(UTC) + timedelta(seconds=3000),
            )
        ]
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None and ref.name == "cachedContents/frompeer"
        assert caches.create_calls == 0

    async def test_expiring_listed_cache_not_adopted(self) -> None:
        from google.genai import types

        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        from langchain_google_genai._function_utils import (
            convert_to_genai_function_declarations,
        )

        from ava_builtins.plugins.lm_google.gemini_cache import _hash_material

        key = _hash_material(
            llm.model, _BIG_PROMPT, convert_to_genai_function_declarations([_fake_tool])
        )
        caches.list_items = [
            types.CachedContent(
                name="cachedContents/dying",
                display_name=f"ava-sys-{llm.model}-{key[:16]}",
                expire_time=datetime.now(UTC) + timedelta(seconds=60),
            )
        ]
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None and ref.name == "cachedContents/fake1"
        assert caches.create_calls == 1


class TestOwnedRequestTimeout:
    """Only an expired owned SDK deadline permits cache-only recovery."""

    @staticmethod
    def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "gemini_cache_timeout_seconds", 0.05)

    async def test_create_hang_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._short_timeout(monkeypatch)
        caches = _HangingCaches(hang_create=True)
        llm = _gemini_llm(caches)
        assert (
            await get_or_create_cache(
                llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )
        assert caches.create_attempts == 1
        # negative memo applies after a timeout, so no retry hits the wire
        assert (
            await get_or_create_cache(
                llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
            )
            is None
        )
        assert caches.create_attempts == 1

    async def test_list_hang_fails_open_to_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hanging list is bounded; the caller then proceeds to create a fresh
        cache instead of being stuck before the first API call."""
        self._short_timeout(monkeypatch)
        caches = _HangingCaches(hang_list=True)
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None and ref.name == "cachedContents/fake1"
        assert caches.list_attempts == 1
        assert caches.create_calls == 1

    async def test_refresh_hang_is_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hanging ttl refresh must not block the memo hit — best-effort means
        the memo entry is still served."""
        self._short_timeout(monkeypatch)
        caches = _HangingCaches(hang_update=True)
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)  # below refresh floor
        ref2 = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref2 is not None and ref2.name == ref.name
        assert caches.update_attempts == 1


@pytest.mark.parametrize("operation", ["create", "list", "update"])
@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError, TimeoutError])
async def test_unknown_cache_error_preserves_identity_without_negative_memo(
    operation: str, error_type: type[Exception]
) -> None:
    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if operation == "update":
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    error = error_type("cache implementation failure")
    error.__dict__["code"] = 429
    error.__cause__ = ClientError(429, {"error": {"code": 429, "message": "quota"}})
    setattr(caches, f"{operation}_error", error)

    with pytest.raises(error_type) as raised:
        await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    assert raised.value is error
    assert not gemini_cache._NEGATIVE
    assert caches.create_calls == (0 if operation == "list" else 1)
    assert caches.list_calls == 1
    assert len(caches.update_calls) == (1 if operation == "update" else 0)

    setattr(caches, f"{operation}_error", None)
    assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy) is not None
    assert caches.create_calls == (2 if operation == "create" else 1)


@pytest.mark.parametrize("operation", ["create", "list", "update"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_cache_rejection_propagates_without_plain_recovery(
    operation: str, status: int
) -> None:
    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if operation == "update":
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    error = ClientError(status, {"error": {"code": status, "message": "request rejected"}})
    setattr(caches, f"{operation}_error", error)

    with pytest.raises(ClientError) as raised:
        await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    assert raised.value is error
    assert not gemini_cache._NEGATIVE
    assert caches.create_calls == (0 if operation == "list" else 1)


@pytest.mark.parametrize("operation", ["create", "list", "update"])
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_typed_transient_cache_rejection_keeps_bounded_recovery(
    operation: str, status: int
) -> None:
    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if operation == "update":
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    error_type = ClientError if status < 500 else ServerError
    setattr(
        caches,
        f"{operation}_error",
        error_type(status, {"error": {"code": status, "message": "temporary rejection"}}),
    )

    result = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    if operation == "create":
        assert result is None
        assert gemini_cache._NEGATIVE
        assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy) is None
        assert caches.create_calls == 1
    else:
        assert result is not None
        assert not gemini_cache._NEGATIVE
        assert caches.create_calls == 1


@pytest.mark.parametrize("operation", ["create", "list", "update"])
async def test_cache_request_external_cancellation_propagates(operation: str) -> None:
    caches = _HangingCaches(**{f"hang_{operation}": True})
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if operation == "update":
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    task = asyncio.create_task(get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy))
    try:
        async with asyncio.timeout(1):
            while getattr(caches, f"{operation}_attempts") == 0:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gemini_cache._NEGATIVE
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("operation", ["create", "list", "update"])
@pytest.mark.parametrize(
    "error_type", [ConnectError, ReadTimeout, ServerDisconnectedError, ServerTimeoutError]
)
async def test_sdk_transport_failure_allows_cache_only_recovery(
    operation: str, error_type: type[Exception]
) -> None:
    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if operation == "update":
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    setattr(caches, f"{operation}_error", error_type("connection interrupted"))

    result = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    assert (result is None) == (operation == "create")
    assert bool(gemini_cache._NEGATIVE) == (operation == "create")
    assert caches.create_calls == 1


@pytest.mark.parametrize("error_type", [ReadTimeout, TypeError, TimeoutError])
async def test_list_metadata_error_is_not_a_transport_recovery(
    error_type: type[Exception],
) -> None:
    error = error_type("cached-content metadata failure")

    class BrokenMetadata:
        @property
        def display_name(self) -> str:
            raise error

    caches = _FakeCaches()
    caches.list_items = [BrokenMetadata()]
    with pytest.raises(error_type) as raised:
        await get_or_create_cache(
            _gemini_llm(caches), _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
    assert raised.value is error
    assert caches.create_calls == 0
    assert not gemini_cache._NEGATIVE


@pytest.mark.parametrize("error_type", [ReadTimeout, TypeError, TimeoutError])
async def test_refresh_response_error_is_not_a_transport_recovery(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    error = error_type("refresh metadata failure")

    class BrokenMetadata:
        @property
        def expire_time(self) -> datetime:
            raise error

    async def update(*, name: str, config: Any) -> BrokenMetadata:
        return BrokenMetadata()

    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    assert ref is not None
    before = datetime.now(UTC) + timedelta(seconds=300)
    ref.expire_time = before
    monkeypatch.setattr(caches, "update", update)
    with pytest.raises(error_type) as raised:
        await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    assert raised.value is error
    assert ref.expire_time == before
    assert not gemini_cache._NEGATIVE


@pytest.mark.parametrize("transient", [False, True])
async def test_paginated_list_failure_uses_the_same_sdk_boundary(transient: bool) -> None:
    caches = _FakeCaches()
    error = ReadTimeout("next page unavailable") if transient else TypeError("page decoder bug")
    caches.page_error = error
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if transient:
        assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy) is not None
        assert caches.create_calls == 1
    else:
        with pytest.raises(TypeError) as raised:
            await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
        assert raised.value is error
        assert caches.create_calls == 0
    assert caches.list_calls == 1
    assert not gemini_cache._NEGATIVE


@pytest.mark.parametrize("name", [None, ""])
async def test_create_response_requires_a_resource_name(
    monkeypatch: pytest.MonkeyPatch,
    name: str | None,
) -> None:
    from google.genai import types

    async def create(*, model: str, config: Any) -> types.CachedContent:
        return types.CachedContent(name=name)

    caches = _FakeCaches()
    monkeypatch.setattr(caches, "create", create)
    with pytest.raises(ValueError, match="no resource name"):
        await get_or_create_cache(
            _gemini_llm(caches), _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
    assert not gemini_cache._MEMO
    assert not gemini_cache._NEGATIVE


async def test_typed_stale_refresh_keeps_the_existing_single_plain_recovery() -> None:
    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy)
    assert ref is not None
    ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    caches.update_error = ClientError(
        403,
        {"error": {"code": 403, "message": "CachedContent not found (or permission denied)"}},
    )
    assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], policy) is ref
    assert caches.update_calls == [ref.name]
    assert caches.create_calls == 1
    assert not gemini_cache._NEGATIVE


@pytest.mark.parametrize("operation", ["create", "list", "update"])
async def test_unknown_cache_prelude_error_stops_the_real_agent_call(operation: str) -> None:
    from agent.llm.cache import ainvoke_with_cache_retry, prepare_invocation
    from ava_builtins.plugins.lm_google.provider import PROVIDER

    caches = _FakeCaches()
    llm = _gemini_llm(caches)
    policy = AgentSlices.resolve().llm_policy
    if operation == "update":
        invocation = await prepare_invocation(
            llm, [SystemMessage(content=_BIG_PROMPT)], policy, PROVIDER.binding
        )
        assert invocation.used_explicit_cache
        assert len(gemini_cache._MEMO) == 1
        ref = next(iter(gemini_cache._MEMO.values()))
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
    error = TypeError("cache prelude implementation failure")
    setattr(caches, f"{operation}_error", error)
    with pytest.raises(TypeError) as raised:
        await ainvoke_with_cache_retry(
            llm, [SystemMessage(content=_BIG_PROMPT)], policy, binding=PROVIDER.binding
        )
    assert raised.value is error
    assert caches.create_calls == (0 if operation == "list" else 1)
    assert not gemini_cache._NEGATIVE


class TestRefresh:
    async def test_near_expiry_triggers_ttl_update(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None
        # age the memo entry to < refresh threshold
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
        ref2 = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref2 is not None
        assert caches.update_calls == [ref.name]
        assert ref.expire_time > datetime.now(UTC) + timedelta(seconds=3000)

    async def test_fresh_entry_no_update(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(
            llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
        assert ref is not None
        await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy)
        assert caches.update_calls == []


class TestStaleAndInvalidate:
    def test_stale_error_shape(self) -> None:
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
        assert is_stale_cache_error(stale)
        bad_request = ClientError(
            400,
            {
                "error": {
                    "code": 400,
                    "message": "Cached content is too small",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
        assert not is_stale_cache_error(bad_request)
        assert not is_stale_cache_error(RuntimeError("nope"))

    def test_invalidate_drops_memo(self) -> None:
        ref = CacheRef(
            name="cachedContents/x", key="k", expire_time=datetime.now(UTC) + timedelta(hours=1)
        )
        gemini_cache._MEMO["k"] = ref
        invalidate(ref)
        assert "k" not in gemini_cache._MEMO


class _NonGeminiLLM(BaseChatModel):
    """A stand-in for a non-Gemini provider (deepseek-via-anthropic etc.)."""

    @property
    def _llm_type(self) -> str:
        return "fake-non-gemini"

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="x"))])


def test_non_gemini_llm_skips_the_heavy_genai_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-Gemini model must not pull the ~66MB google-genai stack into the
    process. get_or_create_cache runs on EVERY LLM call; before the module-name
    pre-check every deepseek/anthropic call paid the import for nothing
    (~66MB resident per process, ~1GB fleet-wide — 2026-08-03 memory audit)."""
    import asyncio
    import sys

    monkeypatch.setattr(settings.lm, "gemini_explicit_cache_enabled", True)
    genai_loaded_before = "langchain_google_genai" in sys.modules

    result = asyncio.run(
        get_or_create_cache(
            _NonGeminiLLM(), _BIG_PROMPT, [_fake_tool], AgentSlices.resolve().llm_policy
        )
    )
    assert result is None

    if not genai_loaded_before:
        assert "langchain_google_genai" not in sys.modules
