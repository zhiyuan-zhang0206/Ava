"""Tests for the lm_google plugin's explicit cachedContents lifecycle.

No API hits: the genai client's async caches surface is faked; the llm is a
real ChatGoogleGenerativeAI (construction is offline) with its client swapped
for the fake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from ava_builtins.plugins.lm_google import gemini_cache
from ava_builtins.plugins.lm_google.gemini_cache import (
    CacheRef,
    get_or_create_cache,
    invalidate,
    is_stale_cache_error,
)
from shared.config import settings


@tool("execute_code", parse_docstring=True)
def _fake_tool(code: str) -> str:
    """Run code.

    Args:
        code: source.
    """
    raise NotImplementedError


_BIG_PROMPT = "You are a test agent. " * 2000  # ~10k chars -> est ~2.5k tokens, above guard
_FAR_FUTURE = datetime.now(UTC) + timedelta(seconds=3600)


class _FakeAsyncPager:
    def __init__(self, items: list[Any]):
        self._items = items

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item


class _FakeCaches:
    """Fake `client.aio.caches` — records calls, scripts failures."""

    def __init__(self) -> None:
        self.create_calls = 0
        self.update_calls: list[str] = []
        self.list_items: list[Any] = []
        self.create_error: Exception | None = None
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
        return _FakeAsyncPager(self.list_items)

    async def update(self, *, name: str, config: Any) -> Any:
        self.update_calls.append(name)
        from google.genai import types

        return types.CachedContent(
            name=name,
            expire_time=datetime.now(UTC) + timedelta(seconds=3600),
        )


class _HangingCaches(_FakeCaches):
    """Fake whose create/list/update hang until cancelled — simulates a wedged
    Gemini API, which the google-genai SDK would otherwise wait on forever.

    The cache layer is fail-open: a timeout must fall back to the plain path
    (None) exactly like any other cache-layer error, never raise into the caller.
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
        assert await get_or_create_cache(_gemini_llm(caches), _BIG_PROMPT, [_fake_tool]) is None
        assert caches.create_calls == 0

    async def test_non_gemini_returns_none(self) -> None:
        assert await get_or_create_cache(_NonGeminiChatModel(), _BIG_PROMPT, [_fake_tool]) is None

    async def test_short_prompt_skipped(self) -> None:
        caches = _FakeCaches()
        assert await get_or_create_cache(_gemini_llm(caches), "tiny prompt", [_fake_tool]) is None
        assert caches.create_calls == 0

    async def test_create_success_and_memo_hit(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref is not None
        assert ref.name == "cachedContents/fake1"
        assert caches.create_calls == 1
        # second call: memo hit, no new create
        ref2 = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref2 is not None and ref2.name == ref.name
        assert caches.create_calls == 1

    async def test_create_failure_negative_memo(self) -> None:
        caches = _FakeCaches()
        caches.create_error = RuntimeError("quota")
        llm = _gemini_llm(caches)
        assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool]) is None
        assert caches.create_calls == 1
        # within the negative window no retry hits the wire
        assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool]) is None
        assert caches.create_calls == 1

    async def test_different_prompt_gets_own_cache(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref_a = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        ref_b = await get_or_create_cache(llm, _BIG_PROMPT + "extra", [_fake_tool])
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
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
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
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref is not None and ref.name == "cachedContents/fake1"
        assert caches.create_calls == 1


class TestTimeoutFailOpen:
    """A wedged Gemini API must not hold the LLM-call prelude: every caches
    call is bounded by AVA_GEMINI_CACHE_TIMEOUT_SECONDS and fails open."""

    @staticmethod
    def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.lm, "gemini_cache_timeout_seconds", 0.05)

    async def test_create_hang_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._short_timeout(monkeypatch)
        caches = _HangingCaches(hang_create=True)
        llm = _gemini_llm(caches)
        assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool]) is None
        assert caches.create_attempts == 1
        # negative memo applies after a timeout, so no retry hits the wire
        assert await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool]) is None
        assert caches.create_attempts == 1

    async def test_list_hang_fails_open_to_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hanging list is bounded; the caller then proceeds to create a fresh
        cache instead of being stuck before the first API call."""
        self._short_timeout(monkeypatch)
        caches = _HangingCaches(hang_list=True)
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref is not None and ref.name == "cachedContents/fake1"
        assert caches.list_attempts == 1
        assert caches.create_calls == 1

    async def test_refresh_hang_is_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hanging ttl refresh must not block the memo hit — best-effort means
        the memo entry is still served."""
        self._short_timeout(monkeypatch)
        caches = _HangingCaches(hang_update=True)
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref is not None
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)  # below refresh floor
        ref2 = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref2 is not None and ref2.name == ref.name
        assert caches.update_attempts == 1


class TestRefresh:
    async def test_near_expiry_triggers_ttl_update(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref is not None
        # age the memo entry to < refresh threshold
        ref.expire_time = datetime.now(UTC) + timedelta(seconds=300)
        ref2 = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref2 is not None
        assert caches.update_calls == [ref.name]
        assert ref.expire_time > datetime.now(UTC) + timedelta(seconds=3000)

    async def test_fresh_entry_no_update(self) -> None:
        caches = _FakeCaches()
        llm = _gemini_llm(caches)
        ref = await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
        assert ref is not None
        await get_or_create_cache(llm, _BIG_PROMPT, [_fake_tool])
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

    result = asyncio.run(get_or_create_cache(_NonGeminiLLM(), _BIG_PROMPT, [_fake_tool]))
    assert result is None

    if not genai_loaded_before:
        assert "langchain_google_genai" not in sys.modules
