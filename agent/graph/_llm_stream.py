"""Streaming consumption for the llm node — the unified streaming-first LLM call.

``_consume_llm`` is the single entry: stream via ``bound_llm.astream(...)``
under per-stage stall timeouts plus a total-attempt ceiling, falling back once
to a non-stream ``ainvoke`` on a stalled stream or a configured-fatal provider
error type.
``_stream_with_cache_retry`` wraps the whole exchange (stale Gemini cache
invalidation + one plain-path retry, concurrency-limiter slot, latency/decode
stamps).

Split out of ``_llm.py`` (Task #1004 >800-line outlier) — the provider-facing
side of the llm node; it feeds chunks into a caller-owned list that
``_llm_chunk`` later assembles.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage

from agent.lm_cache import prepare_invocation
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.log import logger

from ._callbacks import RedisStreamHandler
from ._llm_errors import (
    LLMStreamStallTimeoutError,
    _is_fatal_provider_error_type,
    _parse_provider_error_type,
)


async def _consume_llm(
    bound_llm: object,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
) -> tuple[float | None, float | None]:
    """Unified LLM call entry — streaming-first, falls back once to non-stream on recoverable errors.

    Two fallback triggers:

    1. **LLMStreamStallTimeoutError** — the stream stalled (TTFT or mid-stream
       timeout). On Kimi K3 this often means the OpenAI SDK's internal 429 retry
       got a 200 but the overloaded server never started streaming.

    2. **Fatal provider error types** (e.g. ``engine_overloaded_error`` on Kimi
       K3) — the provider rejected the request outright. The error is detected
       from the provider SDK exception's ``body.error.type`` and matched against
       the configured ``llm_fatal_provider_error_types`` set. If non-streaming
       also fails with the same error, it propagates to ``_llm_node_impl`` →
       ``FatalProviderError`` → fast-fail (no LangGraph retry).

    Note the two paths run under different clocks: the streaming attempt is
    bounded by the per-model TTFT / inter-chunk gap timeouts and total-duration
    ceiling, the fallback by ``llm_non_streaming_fallback_timeout_seconds``
    (600s). Any apparent "streaming fails, non-streaming succeeds" asymmetry
    has to be read against that gap before it is attributed to the provider.

    Fallback runs only once per call — if non-stream also hits the same error,
    propagate naturally. Cost: that turn loses UI progressive streaming display;
    the full turn is returned at once.

    The DeepSeek thinking_delta-loss drift (#167/#168) used to be fallback
    trigger 1 (raised as LLMStreamCorruptedError by chunk validation); it is
    now repaired in place by `_sanitize_thinking_blocks` after final_msg
    assembly — a signature-only block filled with `thinking=""` round-trips
    the endpoint, so no doubled re-request is needed.
    """
    from shared.lm.registry import resolve_setting

    try:
        return await _consume_stream_with_stall_timeout(
            bound_llm.astream(messages).__aiter__(),  # type: ignore[attr-defined]
            chunks=chunks,
            handler=handler,
            # Per-model defaults with shared fallback; explicit env values /
            # the per-agent overlay win (a slow provider gets a longer TTFT
            # without loosening every model's stall detection).
            ttft_timeout=resolve_setting(
                "llm_stream_ttft_timeout_seconds", model=turn_settings.lm.llm_model
            ),
            total_timeout=resolve_setting(
                "llm_stream_total_timeout_seconds", model=turn_settings.lm.llm_model
            ),
            inter_chunk_timeout=resolve_setting(
                "llm_stream_inter_chunk_timeout_seconds", model=turn_settings.lm.llm_model
            ),
        )
    except LLMStreamStallTimeoutError as e:
        # Streaming stalled (e.g. Kimi K3 engine overload: first request 429,
        # retry 200 but server doesn't stream → TTFT timeout). Non-streaming
        # bypasses the SSE event layer entirely — fallback once.
        logger.warning(
            "[{error_type}] retry non-streaming once: {error}",
            event="stream_stalled_retry",
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        chunks.clear()
        return await _ainvoke_single_chunk(
            bound_llm,
            messages,
            chunks=chunks,
            handler=handler,
            timeout=settings.lm.llm_non_streaming_fallback_timeout_seconds,
        )
    except Exception as e:
        # Provider returned a fatal error type (e.g. engine_overloaded_error)
        # on the streaming path. Non-streaming may still succeed (it bypasses
        # the SSE event layer and runs under a far longer timeout). Fallback
        # once before letting the error propagate to _llm_node_impl →
        # FatalProviderError.
        if _is_fatal_provider_error_type(e):
            error_type = _parse_provider_error_type(e) or "unknown"
            logger.warning(
                "[{error_type}] retry non-streaming once: {error}",
                event="stream_overloaded_retry",
                error_type=error_type,
                error=str(e)[:200],
            )
            chunks.clear()
            return await _ainvoke_single_chunk(
                bound_llm,
                messages,
                chunks=chunks,
                handler=handler,
                timeout=settings.lm.llm_non_streaming_fallback_timeout_seconds,
            )
        raise


async def _ainvoke_single_chunk(
    bound_llm: object,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    timeout: float,
) -> tuple[float | None, float | None]:
    """`bound_llm.ainvoke(messages)` single non-stream HTTP fetches the whole
    AIMessage, wrapped as a single chunk and stuffed into chunks list, so the
    caller's chunk-accumulation code does not change.

    `_consume_llm` calls this function as fallback when stream hits
    `LLMStreamCorruptedError`; handler gets one full chunk and publishes
    at once (UI that turn has no progressive display, a corruption-recovery
    trade-off).

    Returns `(None, None)` — a single non-stream HTTP fetch has no
    first-token → last-token window (the whole message arrives at once), so
    `decode_ms` must be NULL for these calls: stamping a fake window (e.g.
    wall-clock) would contaminate the generation-TPS panel with
    non-streaming calls.

    Extracted to module level to reduce `_llm_node_impl`'s statement count
    (PLR0915 50-line cap).
    """
    msg = await asyncio.wait_for(
        bound_llm.ainvoke(messages),  # type: ignore[attr-defined]
        timeout=timeout,
    )
    assert isinstance(msg, AIMessage)  # noqa: S101 — bind_tools returns Runnable but ChatModel still returns AIMessage
    chunk = AIMessageChunk(
        content=msg.content,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        tool_calls=msg.tool_calls or [],  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        additional_kwargs=msg.additional_kwargs,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        response_metadata=msg.response_metadata,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        usage_metadata=msg.usage_metadata,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        id=msg.id,
    )
    chunks.append(chunk)
    handler.process_chunk(chunk)
    return (None, None)


async def _consume_stream_with_stall_timeout(
    stream_iter: AsyncIterator[AIMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    ttft_timeout: float,
    inter_chunk_timeout: float,
    total_timeout: float | None = None,
) -> tuple[float | None, float | None]:
    """`asyncio.wait_for` wraps `__anext__` — raises
    `LLMStreamStallTimeoutError` if no chunk arrives within the timeout.

    Two separate timeouts:
    - `ttft_timeout`: applied to the first chunk (TTFT) — slower models
      (e.g. Gemini) may have >10s cold-start; this value should be higher.
    - `inter_chunk_timeout`: applied to subsequent chunks — mid-stream
      latency should be tight; 10s is the baseline.
    - `total_timeout`: hard ceiling for this streaming attempt, even when every
      individual gap stays healthy. None leaves the attempt unbounded.

    `StopAsyncIteration` is not wrapped in timeout (empty stream should not
    wait N seconds); `chunk_idx` distinguishes TTFT vs mid-stream, giving
    ops different diagnostic signals.

    Also records the stream's decode window: monotonic timestamps of the
    first and last chunk arrival (`first_ts` / `last_ts`), returned as a
    `(first, last)` pair so `_stream_with_cache_retry` can stamp
    `handler.llm_decode_ms = (last - first) * 1000`. An empty stream
    (StopAsyncIteration before any chunk) returns `(None, None)` — there is
    no honest decode window, so the payload carries NULL and the ops panel
    leaves the bucket blank. A stall raise propagates timestamps nowhere
    (the attempt is discarded; only the final successful attempt's window
    counts — same "one logical call" rule as `llm_latency_ms`).

    Extracted to module-level helper: reduces `_llm_node_impl`'s statement
    count (PLR0915) + lets unit tests drive directly
    (`tests/agent/test_llm_stream_stall.py`).
    """
    chunk_idx = 0
    first_ts: float | None = None
    last_ts: float | None = None
    started_at = time.monotonic()
    while True:
        stage_timeout = ttft_timeout if chunk_idx == 0 else inter_chunk_timeout
        timeout = stage_timeout
        total_is_next_deadline = False
        if total_timeout is not None:
            remaining_total = total_timeout - (time.monotonic() - started_at)
            if remaining_total <= 0:
                raise LLMStreamStallTimeoutError(
                    f"LLM stream exceeded {total_timeout:.1f}s total duration "
                    f"after {chunk_idx} chunks; abort streaming attempt."
                )
            if remaining_total <= stage_timeout:
                timeout = remaining_total
                total_is_next_deadline = True
        try:
            chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            if total_timeout is not None and time.monotonic() - started_at >= total_timeout:
                raise LLMStreamStallTimeoutError(
                    f"LLM stream exceeded {total_timeout:.1f}s total duration "
                    f"after {chunk_idx} chunks; abort streaming attempt."
                ) from None
            return (first_ts, last_ts)
        except TimeoutError as e:
            if total_is_next_deadline:
                raise LLMStreamStallTimeoutError(
                    f"LLM stream exceeded {total_timeout:.1f}s total duration "
                    f"after {chunk_idx} chunks; abort streaming attempt."
                ) from e
            stage = "TTFT" if chunk_idx == 0 else f"mid-stream after {chunk_idx} chunks"
            raise LLMStreamStallTimeoutError(
                f"LLM stream stalled — no chunk for {stage_timeout:.1f}s ({stage}); "
                f"abort turn. Provider hang / network drop suspected."
            ) from e
        assert isinstance(chunk, AIMessageChunk)  # noqa: S101
        # Arrival timestamps before fan-out: decode_ms measures the provider's
        # generation window (first token → last token), excluding the
        # synchronous SSE publish cost on our side.
        now = time.monotonic()
        if total_timeout is not None and now - started_at >= total_timeout:
            raise LLMStreamStallTimeoutError(
                f"LLM stream exceeded {total_timeout:.1f}s total duration "
                f"after {chunk_idx} chunks; abort streaming attempt."
            )
        if first_ts is None:
            first_ts = now
        last_ts = now
        chunks.append(chunk)
        chunk_idx += 1
        handler.process_chunk(chunk)


async def _stream_with_cache_retry(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
) -> None:
    """Stream the LLM response into `chunks`, retrying once on a stale cache.

    SystemMessage is in state.messages[0] (injected by claim's first round);
    don't dynamically prepend here — keep byte-level stability so prompt cache
    hits across restart. Handler doesn't go through LangChain callback —
    ChatAnthropic with tools bound never triggers `on_llm_new_token`; see
    _callbacks.py module docstring. Internal streaming/non-streaming branching
    is in `_consume_llm` docstring.

    prepare_invocation picks the request shape: a live explicit Gemini cache
    strips the SystemMessage and binds cached_content (tools come from the
    cache); everything else takes the plain path (bind_tools at use site
    rather than build_chat_model — the factory lives in `shared`, which can't
    import agent.llm.execute_code; bind_tools returns a new Runnable, cheap).
    A stale cache reference 403s on the wire — invalidate the memo and rerun
    once on the plain path.

    Stamps `handler.llm_decode_ms` alongside `llm_latency_ms`: the decode
    window (last chunk - first chunk, ms) of the final successful attempt -
    pure generation time for the Σout/Σdecode_ms TPS panel. Non-streaming
    fallback calls and empty streams leave it None (NULL in the payload).
    """

    # Whole-call wall-clock: starts before the first attempt and is stamped
    # onto the handler after the LAST successful attempt, so the latency
    # covers the stale-cache retry and the non-streaming fallback as one
    # logical LLM call. A raised call never stamps (no llm_usage row exists
    # for it either — usage is logged only on success).
    #
    # The concurrency limiter (AVA_LLM_MAX_CONCURRENT, DeepSeek capped by default)
    # wraps the whole exchange — stream, non-streaming fallback and the
    # stale-cache retry share one slot, so a cap is about in-flight requests
    # to the provider, not about retry attempts.
    async def _run() -> None:
        call_started = time.monotonic()
        invocation = await prepare_invocation(llm, messages)
        try:
            first_ts, last_ts = await _consume_llm(
                invocation.runnable,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                invocation.messages,
                chunks=chunks,
                handler=handler,
            )
        except Exception as exc:
            if invocation.cache_ref is None:
                raise
            from ava_builtins.plugins.lm_google.gemini_cache import (
                CacheRef,
                invalidate,
                is_stale_cache_error,
            )

            if not is_stale_cache_error(exc):
                raise
            cache_ref = cast(CacheRef, invocation.cache_ref)
            logger.warning(
                "[gemini-cache] stale cache {name} — invalidate + retry once on plain path",
                name=cache_ref.name,
            )
            invalidate(cache_ref)
            chunks.clear()
            # The handler's per-stream state (started sets, args bufs, published
            # counts, timers) belongs to the FAILED attempt — re-streaming the
            # same message through it would re-append deltas (doubled partial
            # text) or stall code deltas (concatenated args JSON fails to parse).
            # Reset to a fresh-stream state; msg_idx / agent_id survive by design.
            handler.reset()
            plain = await prepare_invocation(llm, messages)
            first_ts, last_ts = await _consume_llm(
                plain.runnable,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                plain.messages,
                chunks=chunks,
                handler=handler,
            )
        handler.llm_latency_ms = (time.monotonic() - call_started) * 1000.0
        # Decode-stage wall-clock: first-token → last-token arrival from the
        # FINAL successful attempt (`_consume_llm` returns (None, None) for the
        # non-streaming fallback and empty streams → NULL, never a fake window).
        # This is the pure-generation TPS denominator (Σout/Σdecode_ms): it
        # excludes network / queue / prefill that latency_ms still carries.
        handler.llm_decode_ms = (
            (last_ts - first_ts) * 1000.0 if first_ts is not None and last_ts is not None else None
        )

    from shared.lm._concurrency import get_limiter
    from shared.lm.factory import provider_key_of_model

    provider = provider_key_of_model(getattr(llm, "model_name", "") or "")
    async with get_limiter().async_acquire(provider):
        await _run()
