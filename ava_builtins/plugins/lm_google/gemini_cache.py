"""Explicit Gemini context caching (cachedContents API) for the agent system prompt.

This is the ``lm_google`` plugin's explicit-cache wire code; core no longer
owns Gemini wire modules.

Why: implicit caching checkpoints trail a growing conversation, so the fleet
cache-hit rate measurably lags the theoretical ceiling. The system prompt +
the ``execute_code`` tool schema are static for a process boot, so they can be
pinned into a server-side ``CachedContent`` once and referenced by every later
request — the cached share no longer depends on the implicit checkpoint
cadence, and cache-read tokens bill at ~0.1x input (cache storage bills per
token-hour; a ~10-20k-token prompt costs fractions of a cent per hour).

Status (task #2660, 2026-09-09): this path is OPT-IN — the
``AVA_GEMINI_EXPLICIT_CACHE_ENABLED`` default flipped to False. With an
explicit cache attached, Gemini's ``cachedContentTokenCount`` reports only
the explicit block (system+tools); implicit hits on the conversation tail are
billed but not reported, so the reported hit-rate share shrinks as
conversations grow. Implicit-only (the default) reports the real full-prefix
hits. Enable this path only for workloads dominated by a huge stable system
prompt; when it rides, the usage event is labeled
``cache_mechanism=mixed, cache_scope=explicit_block``.

Wire contract (verified live 2026-07-25 against gemini-3.6-flash; carries
over to gemini-3.7-flash — same 3.x flash wire; re-verify if the 400/403
shapes drift):

- The cache carries ``system_instruction`` + ``tools``. A generate request
  referencing it must NOT set system_instruction / tools / tool_config (the
  API 400s: "CachedContent can not be used with GenerateContent request
  setting system_instruction, tools or tool_config") — callers strip the
  SystemMessage and skip bind_tools. Tool calling still works: the
  declarations come from the cache.
- Minimum cached size is 1024 tokens; below it create 400s with
  "Cached content is too small". ``_MIN_TOKENS_GUARD`` estimates ahead and
  skips tiny prompts so the guard error never reaches the wire.
- A stale/expired cache reference fails the request with 403
  PERMISSION_DENIED ("CachedContent not found (or permission denied)") —
  ``is_stale_cache_error`` detects that shape so the caller can invalidate
  and retry once on the plain path.
- TTL is created at 3600s; ``caches.update`` extends it (only ttl/expire_time
  are mutable). Refresh runs when remaining lifetime drops below
  ``_REFRESH_BELOW_SECONDS``.

Sharing: the system prompt is byte-stable across same-build agents (the
it carries no per-agent id — fork-safe; per-agent notes ride as
separate HumanMessages), so one cache per (model, prompt+tool hash) serves
every agent on the API key. Processes adopt each other's caches via
``caches.list()`` matched on display_name ``ava-sys-{model}-{key16}``; the
process-local memo keeps the steady state at zero extra API calls per turn.

Cache requests recover only from trusted transient provider errors, typed
connection/timeouts, and an expired owned deadline. Unknown errors and permanent
rejections propagate unchanged. Schema and response processing do not authorize
recovery. A known stale-cache refresh leaves the reference for the existing
single plain-call recovery; other refresh failures are not treated as success.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage, SystemMessage
from loguru import logger

from base.host.env.agent_slices import LlmCallPolicy
from base.lm.call import LlmInvocation, ProviderCallContext
from base.lm.errors import ErrorClass, classify_error

# Cache lifetime. 3600s is also the API default; stated explicitly so the
# refresh arithmetic has one source. Storage bills per token-hour, so a
# longer TTL would only bill more for idle agents.
_CACHE_TTL_SECONDS = 3600

# Refresh when the entry has less than this left. One update call per ~45min
# of continuous activity per process; a turn never sees a mid-flight expiry.
_REFRESH_BELOW_SECONDS = 900

# Don't adopt a listed cache that dies almost immediately — creating a fresh
# one is cheaper than adopting + refreshing + racing its expiry.
_ADOPT_MIN_REMAINING_SECONDS = 300

# After a declared transient create failure, skip creation for this long
# (per key) so an unavailable cache costs one request per window, not per turn.
_NEGATIVE_RETRY_SECONDS = 600.0

# Skip caching when the prompt estimates below this. The API floor is 1024
# tokens; chars/4 underestimates real BPE counts on English prose, and the
# guard only needs to keep the "too small" 400 off the wire.
_MIN_TOKENS_GUARD = 2048


@dataclass
class CacheRef:
    """Handle for one live explicit cache, returned by ``get_or_create_cache``.

    ``name`` is the server-side identifier (``cachedContents/...``) bound onto
    requests; ``key`` identifies the (model, prompt, tools) hash for
    ``invalidate``. ``expire_time`` is tz-aware UTC.
    """

    name: str
    key: str
    expire_time: datetime


_MEMO: dict[str, CacheRef] = {}
_NEGATIVE: dict[str, float] = {}  # key -> time.monotonic() deadline to skip creation until


class _CacheUnavailableError(Exception):
    """A declared transient SDK request failure permits cache-only recovery."""


async def _cache_request[T](operation: Awaitable[T], timeout_s: float) -> T:
    """Bound actual SDK I/O; application errors retain their original identity."""
    from aiohttp import ClientConnectionError, ClientSSLError, ServerTimeoutError
    from google.auth.exceptions import TransportError
    from google.genai.errors import APIError
    from httpx import NetworkError, RemoteProtocolError, TimeoutException

    deadline = asyncio.timeout(timeout_s)
    try:
        async with deadline:
            return await operation
    except TimeoutError as exc:
        if not deadline.expired() and not isinstance(exc, ServerTimeoutError):
            raise
        raise _CacheUnavailableError("cache request timed out") from exc
    except ClientSSLError:
        raise
    except APIError as exc:
        if classify_error(exc).error_class is not ErrorClass.TRANSIENT:
            raise
        raise _CacheUnavailableError("transient cache provider rejection") from exc
    except (
        NetworkError,
        RemoteProtocolError,
        TimeoutException,
        ClientConnectionError,
        TransportError,
    ) as exc:
        raise _CacheUnavailableError("cache transport unavailable") from exc


def _remaining_seconds(ref: CacheRef, now: datetime) -> float:
    return (ref.expire_time - now).total_seconds()


def _hash_material(model: str, system_text: str, genai_tools: list[Any]) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(system_text.encode())
    h.update(b"\x00")
    for tool in genai_tools:
        h.update(tool.model_dump_json(exclude_none=True).encode())
        h.update(b"\x00")
    return h.hexdigest()


def is_stale_cache_error(exc: BaseException) -> bool:
    """True when `exc` is the Gemini 403 for a missing/expired cachedContent.

    google.genai.errors.ClientError carries ``code`` (403) and the message
    "CachedContent not found (or permission denied)" — expiry deletes the
    object server-side, so a lapsed TTL lands here exactly like a bogus name.
    Only trusted provider types (including official model wrappers) authorize
    recovery; matching attributes on an application error do not.
    """
    classified = classify_error(exc)
    return (
        classified.error_class is ErrorClass.PERMANENT
        and classified.status == 403
        and "CachedContent not found" in str(exc)
    )


def invalidate(ref: CacheRef) -> None:
    """Drop the memo entry for `ref` after a stale-cache failure.

    Process-local only: the server-side object is already gone (that is why
    the caller is here), and other processes re-adopt or recreate on their
    next turn.
    """
    _MEMO.pop(ref.key, None)


async def _maybe_refresh(client: Any, ref: CacheRef, now: datetime, timeout_s: float) -> None:
    """Extend the TTL; only declared transient/stale failures leave the reference."""
    if _remaining_seconds(ref, now) >= _REFRESH_BELOW_SECONDS:
        return
    from google.genai import types
    from google.genai.errors import APIError

    try:
        updated = await _cache_request(
            client.aio.caches.update(
                name=ref.name,
                config=types.UpdateCachedContentConfig(ttl=f"{_CACHE_TTL_SECONDS}s"),
            ),
            timeout_s,
        )
    except (_CacheUnavailableError, APIError) as exc:
        if isinstance(exc, APIError) and not is_stale_cache_error(exc):
            raise
        logger.opt(exception=True).warning(
            "[gemini-cache] ttl refresh failed for {name}; the entry stays and a stale cache "
            "recovers on the next request",
            name=ref.name,
        )
        return
    ref.expire_time = updated.expire_time or (now + timedelta(seconds=_CACHE_TTL_SECONDS))


async def _adopt_existing(
    client: Any, model: str, key: str, now: datetime, timeout_s: float
) -> CacheRef | None:
    """Adopt a cache created by another process of this build, matched on the
    display_name convention. A declared transient list failure or no live match
    permits a fresh create; unknown failures stop the call."""
    display = f"ava-sys-{model}-{key[:16]}"
    expires_at = time.monotonic() + timeout_s
    try:
        pager = await _cache_request(client.aio.caches.list(), timeout_s)
        iterator = pager.__aiter__()
        while True:
            try:
                cached = await _cache_request(
                    anext(iterator), max(0.0, expires_at - time.monotonic())
                )
            except StopAsyncIteration:
                return None
            if cached.display_name != display or cached.name is None:
                continue
            expire = cached.expire_time
            if expire is None:
                continue
            if (expire - now).total_seconds() < _ADOPT_MIN_REMAINING_SECONDS:
                continue
            ref = CacheRef(name=cached.name, key=key, expire_time=expire)
            logger.info(
                "[gemini-cache] adopted existing cache {name} (expires {expire})",
                name=ref.name,
                expire=expire,
            )
            return ref
    except _CacheUnavailableError:
        logger.opt(exception=True).warning(
            "[gemini-cache] listing existing caches failed; creating a fresh one instead"
        )
    return None


def _eligible_gemini(llm: BaseChatModel, system_text: str) -> Any | None:
    """The Gemini chat model when an explicit cache is worth trying for this prompt, else None."""
    # Cheap pre-check before the ~66MB google-genai import: this function runs
    # on EVERY LLM call, and for non-Gemini providers the import was pure waste
    # (deepseek/anthropic models can never be ChatGoogleGenerativeAI) — ~66MB
    # resident per process, ~1GB across the fleet (2026-08-03 memory audit).
    if "langchain_google_genai" not in type(llm).__module__:
        return None
    from langchain_google_genai import ChatGoogleGenerativeAI

    if not isinstance(llm, ChatGoogleGenerativeAI):
        return None
    if llm.client is None or not system_text:
        return None
    if len(system_text) // 4 < _MIN_TOKENS_GUARD:
        logger.debug(
            "[gemini-cache] prompt ~{est} tokens below guard {guard} — implicit caching only",
            est=len(system_text) // 4,
            guard=_MIN_TOKENS_GUARD,
        )
        return None
    return llm


async def _create_cache(
    llm: Any, system_text: str, genai_tools: Any, key: str, policy: LlmCallPolicy
) -> Any | None:
    """Create a cache; only declared transient failures stamp the negative memo."""
    from google.genai import types

    try:
        cache = await _cache_request(
            llm.client.aio.caches.create(
                model=llm.model,
                config=types.CreateCachedContentConfig(
                    display_name=f"ava-sys-{llm.model}-{key[:16]}",
                    system_instruction=types.Content(
                        parts=[types.Part.from_text(text=system_text)]
                    ),
                    tools=genai_tools,
                    ttl=f"{_CACHE_TTL_SECONDS}s",
                ),
            ),
            policy.gemini_cache_timeout_seconds,
        )
    except _CacheUnavailableError:
        _NEGATIVE[key] = time.monotonic() + _NEGATIVE_RETRY_SECONDS
        logger.opt(exception=True).warning(
            "[gemini-cache] transient create failure (implicit caching only for {window}s)",
            window=int(_NEGATIVE_RETRY_SECONDS),
        )
        return None
    if cache is None or not cache.name:
        raise ValueError("Google explicit cache creation returned no resource name")
    return cache


async def get_or_create_cache(
    llm: BaseChatModel,
    system_text: str,
    tools: list[Any],
    policy: LlmCallPolicy,
) -> CacheRef | None:
    """Return a live explicit cache for (llm.model, system_text, tools), or None.

    None means "use the plain path" — non-Gemini model, feature flag off,
    prompt below the token floor, or a declared transient request failure.
    Unknown errors propagate. Callers use SystemMessage in-band + bind_tools
    when caching is unavailable under this explicit contract.

    `tools` are LangChain tools (``[execute_code]``); their converted schema
    is baked into the cache, so cache-bound requests must NOT bind tools.
    """
    if not policy.gemini_explicit_cache_enabled:
        return None
    gemini = _eligible_gemini(llm, system_text)
    if gemini is None:
        return None
    from langchain_google_genai._function_utils import (
        convert_to_genai_function_declarations,
    )

    genai_tools = convert_to_genai_function_declarations(tools)
    key = _hash_material(gemini.model, system_text, genai_tools)
    now = datetime.now(UTC)

    memo = _MEMO.get(key)
    if memo is not None:
        if _remaining_seconds(memo, now) > 60:
            await _maybe_refresh(gemini.client, memo, now, policy.gemini_cache_timeout_seconds)
            return memo
        _MEMO.pop(key, None)

    neg_until = _NEGATIVE.get(key)
    if neg_until is not None:
        if time.monotonic() < neg_until:
            return None
        _NEGATIVE.pop(key, None)

    adopted = await _adopt_existing(
        gemini.client, gemini.model, key, now, policy.gemini_cache_timeout_seconds
    )
    if adopted is not None:
        _MEMO[key] = adopted
        await _maybe_refresh(gemini.client, adopted, now, policy.gemini_cache_timeout_seconds)
        return adopted

    cache = await _create_cache(gemini, system_text, genai_tools, key, policy)
    if cache is None:
        return None

    ref = CacheRef(
        name=cache.name,
        key=key,
        expire_time=cache.expire_time or (now + timedelta(seconds=_CACHE_TTL_SECONDS)),
    )
    _MEMO[key] = ref
    tokens = cache.usage_metadata.total_token_count if cache.usage_metadata else None
    logger.info(
        "[gemini-cache] created {name}: {tokens} tokens cached, expires {expire}",
        name=ref.name,
        tokens=tokens,
        expire=ref.expire_time,
    )
    return ref


async def prepare_call(ctx: ProviderCallContext) -> LlmInvocation | None:
    """Prepare Google wire shape and capture this attempt's opaque cache handle."""
    if not isinstance(ctx.provider_config, LlmCallPolicy):
        raise TypeError("Google call requires a resolved LlmCallPolicy snapshot")
    messages = cast(list[AnyMessage], ctx.messages)
    if not (
        messages
        and isinstance(messages[0], SystemMessage)
        and isinstance(messages[0].content, str)
        and not any(isinstance(message, SystemMessage) for message in messages[1:])
    ):
        return None
    llm = cast(BaseChatModel, ctx.llm)
    ref = await get_or_create_cache(llm, messages[0].content, ctx.tools, ctx.provider_config)
    if ref is None:
        return None

    def recover(exc: BaseException) -> LlmInvocation | None:
        if not isinstance(exc, Exception):
            raise exc
        if not is_stale_cache_error(exc):
            return None
        logger.warning(
            "[gemini-cache] stale cache {name} — invalidate + retry once on plain path",
            name=ref.name,
        )
        invalidate(ref)
        return LlmInvocation(
            runnable=llm.bind_tools(cast(list[Any], ctx.tools)), messages=list(messages)
        )

    return LlmInvocation(
        runnable=llm.bind(cached_content=ref.name),
        messages=list(messages[1:]),
        used_explicit_cache=True,
        recover=recover,
    )
