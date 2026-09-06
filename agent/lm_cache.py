"""LLM invocation prep: explicit Gemini cache binding with a plain-path fallback.

Shared by the llm node (streaming) and the compaction path (single invoke) so
every request an agent sends picks the same shape:

- cache path — the system prompt + execute_code schema live in a server-side
  CachedContent (ava_builtins/plugins/lm_google/gemini_cache.py); the request strips the leading
  SystemMessage and binds `cached_content` instead of tools (the API 400s if
  tools/system_instruction ride alongside a cache reference).
- plain path — today's behavior: SystemMessage in-band + bind_tools. Taken for
  non-Gemini models, when the flag is off, and on any cache-layer failure
  (get_or_create_cache is fail-open).

`invoke_with_cache_retry` wraps the whole exchange for single-shot callers
(compaction): on a stale-cache 403 it invalidates the memo and retries once on
the plain path. The llm node runs the same pattern around its streaming
_consume_llm call (it owns chunk/handler wiring, so it can't share this
function).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.runnables import Runnable
from loguru import logger

from agent.llm import execute_code


class CacheRef(Protocol):
    """Agent-visible cache handle without importing provider wire code eagerly."""

    name: str


@dataclass
class LlmInvocation:
    """One prepared LLM call: the runnable to invoke + the messages to send.

    `cache_ref` is None on the plain path; non-None marks a cache-bound call
    so the caller can scope its stale-cache retry to the path that can raise it.
    """

    runnable: Runnable
    messages: list[AnyMessage]
    cache_ref: CacheRef | None


async def prepare_invocation(
    llm: BaseChatModel,
    messages: list[AnyMessage],
) -> LlmInvocation:
    """Bind `llm` for one request — via the explicit Gemini cache when possible.

    Falls back to the plain path (bind_tools + unmodified messages) whenever
    caching does not apply: non-Gemini model, flag off, below the token floor,
    cache-layer error, or a message list whose head is not a single text
    SystemMessage (defensive — claim/compact guarantee it today).
    """
    if (
        messages
        and isinstance(messages[0], SystemMessage)
        and isinstance(messages[0].content, str)  # pyright: ignore[reportUnknownMemberType]
        and not any(isinstance(m, SystemMessage) for m in messages[1:])
    ):
        from ava_builtins.plugins.lm_google.gemini_cache import get_or_create_cache

        cache_ref = await get_or_create_cache(llm, messages[0].content, [execute_code])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        if cache_ref is not None:
            return LlmInvocation(
                runnable=llm.bind(cached_content=cache_ref.name),  # pyright: ignore[reportUnknownMemberType]
                messages=list(messages[1:]),
                cache_ref=cache_ref,
            )
    return LlmInvocation(
        runnable=llm.bind_tools([execute_code]),  # pyright: ignore[reportUnknownMemberType]
        messages=list(messages),
        cache_ref=None,
    )


async def ainvoke_with_cache_retry(llm: BaseChatModel, messages: list[AnyMessage]) -> AIMessage:
    """Single-shot invoke through `prepare_invocation`, with one stale-cache retry.

    A cache can die between preparation and the wire (TTL lapse); the API
    403s with "CachedContent not found". Invalidate the memo and rerun once on
    the plain path — the turn pays full price once instead of failing.

    The whole exchange (prepare + invoke + stale-cache retry) is bounded by
    `settings.lm.llm_compact_timeout_seconds` (default 120s): compaction runs
    inside the agent's claim node where the agent cannot answer inbound, so a
    wedged provider must surface as a failed compact — the agent stays alive
    and retries later — rather than holding the turn at the provider SDK's
    default (~600s). The 403-stale branch keeps its own single retry inside
    the bound.
    """
    import asyncio

    from shared.config import settings

    async def _invoke() -> AIMessage:
        invocation = await prepare_invocation(llm, messages)
        try:
            response = await invocation.runnable.ainvoke(invocation.messages)  # pyright: ignore[reportUnknownMemberType]
        except Exception as exc:
            if invocation.cache_ref is None:
                raise
            from ava_builtins.plugins.lm_google.gemini_cache import (
                CacheRef as GeminiCacheRef,
            )
            from ava_builtins.plugins.lm_google.gemini_cache import (
                invalidate,
                is_stale_cache_error,
            )

            if not is_stale_cache_error(exc):
                raise
            cache_ref = cast(GeminiCacheRef, invocation.cache_ref)
            logger.warning(
                "[gemini-cache] stale cache {name} — invalidate + retry once on plain path",
                name=cache_ref.name,
            )
            invalidate(cache_ref)
            plain = await prepare_invocation(llm, messages)
            response = await plain.runnable.ainvoke(plain.messages)  # pyright: ignore[reportUnknownMemberType]
        assert isinstance(response, AIMessage)  # noqa: S101 — chat models return AIMessage
        return response

    return await asyncio.wait_for(_invoke(), timeout=settings.lm.llm_compact_timeout_seconds)
