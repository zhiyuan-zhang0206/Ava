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
``{YOUR_AGENT_ID}`` placeholder keeps it fork-safe; per-agent notes ride as
separate HumanMessages), so one cache per (model, prompt+tool hash) serves
every agent on the API key. Processes adopt each other's caches via
``caches.list()`` matched on display_name ``ava-sys-{model}-{key16}``; the
process-local memo keeps the steady state at zero extra API calls per turn.

Fail-open by design: every cache-layer error (quota, network, unsupported
model, below-minimum) logs and returns None — the caller falls back to the
implicit-caching path (SystemMessage in-band + bind_tools), so this feature
can only ever cost an occasional extra API call, never break a turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from shared.config.turn_view import turn_settings

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

# After a create/list failure, skip retries for this long (per key) so a
# broken cache layer costs one API call per window, not one per turn.
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
    """
    return getattr(exc, "code", None) == 403 and "CachedContent not found" in str(exc)


def invalidate(ref: CacheRef) -> None:
    """Drop the memo entry for `ref` after a stale-cache failure.

    Process-local only: the server-side object is already gone (that is why
    the caller is here), and other processes re-adopt or recreate on their
    next turn.
    """
    _MEMO.pop(ref.key, None)


async def _maybe_refresh(client: Any, ref: CacheRef, now: datetime) -> None:
    """Extend the TTL when the entry is close to expiring. Best-effort: a
    failed refresh leaves the entry in place — if the cache really died, the
    next request 403s and the caller's stale-retry recovers."""
    if _remaining_seconds(ref, now) >= _REFRESH_BELOW_SECONDS:
        return
    from google.genai import types

    try:
        updated = await asyncio.wait_for(
            client.aio.caches.update(
                name=ref.name,
                config=types.UpdateCachedContentConfig(ttl=f"{_CACHE_TTL_SECONDS}s"),
            ),
            timeout=turn_settings.lm.gemini_cache_timeout_seconds,
        )
        ref.expire_time = updated.expire_time or (now + timedelta(seconds=_CACHE_TTL_SECONDS))
    except Exception as exc:
        logger.debug(
            "[gemini-cache] ttl refresh failed for {name}: {exc!r}", name=ref.name, exc=exc
        )


async def _adopt_existing(client: Any, model: str, key: str, now: datetime) -> CacheRef | None:
    """Adopt a cache created by another process of this build, matched on the
    display_name convention. Returns None when the list call fails or nothing
    live matches — the caller then creates a fresh cache."""
    display = f"ava-sys-{model}-{key[:16]}"

    async def _scan() -> CacheRef | None:
        async for cached in await client.aio.caches.list():
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
        return None

    try:
        return await asyncio.wait_for(
            _scan(), timeout=turn_settings.lm.gemini_cache_timeout_seconds
        )
    except Exception as exc:
        logger.debug("[gemini-cache] list failed (will create instead): {exc!r}", exc=exc)
    return None


async def get_or_create_cache(
    llm: BaseChatModel,
    system_text: str,
    tools: list[Any],
) -> CacheRef | None:
    """Return a live explicit cache for (llm.model, system_text, tools), or None.

    None means "use the plain path" — non-Gemini model, feature flag off,
    prompt below the token floor, or any cache-layer error (logged). Callers
    treat None as today's behavior: SystemMessage in-band + bind_tools.

    `tools` are LangChain tools (``[execute_code]``); their converted schema
    is baked into the cache, so cache-bound requests must NOT bind tools.
    """
    if not turn_settings.lm.gemini_explicit_cache_enabled:
        return None
    # Cheap pre-check before the ~66MB google-genai import: this function runs
    # on EVERY LLM call, and for non-Gemini providers the import was pure waste
    # (deepseek/anthropic models can never be ChatGoogleGenerativeAI) — ~66MB
    # resident per process, ~1GB across the fleet (2026-08-03 memory audit).
    if "langchain_google_genai" not in type(llm).__module__:
        return None
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_google_genai._function_utils import (
        convert_to_genai_function_declarations,
    )

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

    genai_tools = convert_to_genai_function_declarations(tools)
    key = _hash_material(llm.model, system_text, genai_tools)
    now = datetime.now(UTC)

    memo = _MEMO.get(key)
    if memo is not None:
        if _remaining_seconds(memo, now) > 60:
            await _maybe_refresh(llm.client, memo, now)
            return memo
        _MEMO.pop(key, None)

    neg_until = _NEGATIVE.get(key)
    if neg_until is not None:
        if time.monotonic() < neg_until:
            return None
        _NEGATIVE.pop(key, None)

    adopted = await _adopt_existing(llm.client, llm.model, key, now)
    if adopted is not None:
        _MEMO[key] = adopted
        await _maybe_refresh(llm.client, adopted, now)
        return adopted

    from google.genai import types

    try:
        cache = await asyncio.wait_for(
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
            timeout=turn_settings.lm.gemini_cache_timeout_seconds,
        )
    except Exception as exc:
        _NEGATIVE[key] = time.monotonic() + _NEGATIVE_RETRY_SECONDS
        logger.warning(
            "[gemini-cache] create failed (implicit caching only for {window}s): {exc!r}",
            window=int(_NEGATIVE_RETRY_SECONDS),
            exc=exc,
        )
        return None

    if cache.name is None:
        _NEGATIVE[key] = time.monotonic() + _NEGATIVE_RETRY_SECONDS
        logger.warning("[gemini-cache] create returned no name — implicit caching only")
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
