"""Streaming consumption for the llm node — the unified streaming-first LLM call.

``_consume_llm`` is the single entry: stream via ``bound_llm.astream(...)``
under per-stage stall timeouts plus a total-attempt ceiling, falling back once
to a non-stream ``ainvoke`` on a stalled stream or a configured-fatal provider
error type. A stalled stream's fallback runs under the SAME bound as its
stream segment; when that fallback also times out (two adjacent stalls), the
call is terminated as ``LLMStreamStallPairError`` for the delayed retry
schedule instead of burning the provider's stalled segments.
``_stream_llm`` binds the agent tool and stamps whole-call latency and decode timing.

Split out of ``node.py`` (Task #1004 >800-line outlier) — the provider-facing
side of the llm node; it feeds chunks into a caller-owned list that
``_chunk`` later assembles.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable
from typing import cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage
from langchain_core.runnables import Runnable

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm_errors import (
    LLMStreamStallPairError,
    LLMStreamStallTimeoutError,
    _is_fatal_provider_error_type,
    _parse_provider_error_type,
)
from agent.llm import execute_code
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.errors import normalize_provider_transport_error
from base.log import logger


class _ModelWaitTimeoutError(TimeoutError):
    """The model await exceeded its owned deadline; unrelated timeouts do not."""


async def _await_model_message(operation: Awaitable[AIMessage], timeout: float) -> AIMessage:
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            return await operation
    except TimeoutError as exc:
        if deadline.expired():
            raise _ModelWaitTimeoutError(f"Model wait exceeded {timeout:g}s") from exc
        raise
    except Exception as exc:
        normalized = normalize_provider_transport_error(exc)
        if normalized is exc:
            raise
        raise normalized from exc


async def _consume_llm(
    bound_llm: object,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    agent: AgentSlices,
    catalog: ModelCatalog,
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
    ceiling; the stall-triggered fallback runs under the SAME
    ``llm_stream_ttft_timeout_seconds`` resolution as its stream segment (one
    key, one value — a stalled call costs ~2x the bound, never the stream
    bound plus a separate 600s ceiling), while the fatal-error-type fallback
    keeps ``llm_non_streaming_fallback_timeout_seconds`` (that trigger is not
    a stall; the fallback may bypass the SSE layer and complete a full slow
    generation). Any apparent "streaming fails, non-streaming succeeds"
    asymmetry has to be read against these bounds before it is attributed to
    the provider.

    A stall whose fallback ALSO times out is a *stall pair* — the fallback's
    owned deadline expiry is re-raised as ``LLMStreamStallPairError`` here, so the
    retry policy's delayed stall schedule (not the generic transient fast
    retry) owns the next attempt, and a bare transport TimeoutError never
    reads as one more blip to re-burn.

    Fallback runs only once per call — if non-stream also hits the same error,
    propagate naturally. Cost: that turn loses UI progressive streaming display;
    the full turn is returned at once.

    The DeepSeek thinking_delta-loss drift (#167/#168) used to be fallback
    trigger 1 (raised as LLMStreamCorruptedError by chunk validation); it is
    now repaired in place by `_sanitize_thinking_blocks` after final_msg
    assembly — a signature-only block filled with `thinking=""` round-trips
    the endpoint, so no doubled re-request is needed.
    """
    from base.lm.factory import provider_key_of_model
    from base.lm.registry import resolve_setting

    model = agent.brain.llm_model
    # One resolution feeds BOTH segments of a stalled call: the stream
    # segment's first-chunk bound and the post-stall non-streaming fallback.
    # Per-model defaults with shared fallback; explicit env values / the
    # per-agent overlay win (a slow provider gets a longer bound without
    # loosening every model's stall detection).
    stall_segment_timeout = resolve_setting(
        "llm_stream_ttft_timeout_seconds",
        model=model,
        models=catalog.models,
        explicit=agent.overrides.llm_stream_ttft_timeout_seconds,
    )
    stream_started = time.monotonic()
    try:
        return await _consume_stream_with_stall_timeout(
            bound_llm.astream(messages).__aiter__(),  # type: ignore[attr-defined]
            chunks=chunks,
            handler=handler,
            ttft_timeout=stall_segment_timeout,
            total_timeout=resolve_setting(
                "llm_stream_total_timeout_seconds",
                model=model,
                models=catalog.models,
                explicit=agent.overrides.llm_stream_total_timeout_seconds,
            ),
            inter_chunk_timeout=resolve_setting(
                "llm_stream_inter_chunk_timeout_seconds",
                model=model,
                models=catalog.models,
                explicit=agent.overrides.llm_stream_inter_chunk_timeout_seconds,
            ),
        )
    except LLMStreamStallTimeoutError as e:
        # Streaming stalled (e.g. Kimi K3 engine overload: first request 429,
        # retry 200 but server doesn't stream → TTFT timeout). Non-streaming
        # bypasses the SSE event layer entirely — fallback once, under the SAME
        # bound (`stall_segment_timeout`): a stalled provider then costs ~2x the
        # bound for the whole call instead of the stream bound plus the 600s
        # fallback ceiling, and the second timeout terminates the pair instead
        # of burning further (see below).
        logger.warning(
            "[{error_type}] retry non-streaming once: {error}",
            event="stream_stalled_retry",
            error_type=type(e).__name__,
            error=str(e)[:200],
            vendor=provider_key_of_model(model, catalog=catalog),
            model=model,
            stage=e.stage,
            elapsed_s=round(time.monotonic() - stream_started, 1),
        )
        chunks.clear()
        try:
            return await _ainvoke_single_chunk(
                bound_llm,
                messages,
                chunks=chunks,
                handler=handler,
                timeout=stall_segment_timeout,
            )
        except _ModelWaitTimeoutError as fallback_timeout:
            # Second adjacent stall: the non-streaming retry was not served
            # either, inside the same bound. Terminate the call here as a
            # first-class LLMStreamError so the delayed stall schedule (not the
            # generic transient retry) owns the next attempt. A bare timeout
            # has no authority to enter that owned stall schedule.
            logger.warning(
                "two adjacent stalls — non-streaming fallback timed out after "
                "{timeout_s:.1f}s; terminating the call for a delayed retry",
                event="stream_stall_pair_terminated",
                vendor=provider_key_of_model(model, catalog=catalog),
                model=model,
                stage=e.stage,
                timeout_s=stall_segment_timeout,
            )
            raise LLMStreamStallPairError(
                f"LLM stream stalled ({e.stage}) and the non-streaming fallback also "
                f"timed out after {stall_segment_timeout:.1f}s — two adjacent stalls; "
                f"aborting the call for a delayed retry.",
                stage=e.stage,
            ) from fallback_timeout
    except Exception as e:
        # Provider returned a fatal error type (e.g. engine_overloaded_error)
        # on the streaming path. Non-streaming may still succeed (it bypasses
        # the SSE event layer and runs under a far longer timeout). Fallback
        # once before letting the error propagate to _llm_node_impl →
        # FatalProviderError. This trigger is NOT a stall, so the pair bound
        # above does not apply — the fallback keeps
        # `llm_non_streaming_fallback_timeout_seconds` as its ceiling.
        if _is_fatal_provider_error_type(e, agent.llm_policy):
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
                timeout=agent.read("lm", "llm_non_streaming_fallback_timeout_seconds"),
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

    `_consume_llm` uses this once for an owned stall or a configured provider
    overload. The handler publishes one full chunk without progressive display.

    Returns `(None, None)` — a single non-stream HTTP fetch has no
    first-token → last-token window (the whole message arrives at once), so
    `decode_ms` must be NULL for these calls: stamping a fake window (e.g.
    wall-clock) would contaminate the generation-TPS panel with
    non-streaming calls.

    Extracted to module level to reduce `_llm_node_impl`'s statement count
    (PLR0915 50-line cap).
    """
    runnable = cast(Runnable[list[AnyMessage], AIMessage], bound_llm)
    msg = await _await_model_message(runnable.ainvoke(messages), timeout)
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
    """An owned `asyncio.timeout` deadline wraps only the model's `__anext__`.
    Expiry raises `LLMStreamStallTimeoutError`; ordinary model/callback
    `TimeoutError` retains its identity without claiming that timer expired.

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
    `(first, last)` pair so `_stream_llm` can stamp
    `handler.llm_decode_ms = (last - first) * 1000`. An empty stream
    (StopAsyncIteration before any chunk) returns `(None, None)` — there is
    no honest decode window, so the payload carries NULL and the ops panel
    leaves the bucket blank. A stall raise propagates timestamps nowhere
    (the attempt is discarded; only the final successful attempt's window
    counts — same "one logical call" rule as `llm_latency_ms`).

    Extracted to module-level helper: reduces `_llm_node_impl`'s statement
    count (PLR0915) + lets unit tests drive directly
    (`agent/graph/llm/tests/test_llm_stream_stall.py`).
    """
    chunk_idx = 0
    first_ts: float | None = None
    last_ts: float | None = None
    started_at = time.monotonic()
    while True:
        stage_timeout = ttft_timeout if chunk_idx == 0 else inter_chunk_timeout
        timeout, total_is_next_deadline = _next_timeout(
            stage_timeout, total_timeout, started_at, chunk_idx
        )
        try:
            chunk = await _await_model_message(stream_iter.__anext__(), timeout)
        except StopAsyncIteration:
            if total_timeout is not None and time.monotonic() - started_at >= total_timeout:
                raise _total_stall(total_timeout, chunk_idx) from None
            return (first_ts, last_ts)
        except _ModelWaitTimeoutError as e:
            if total_is_next_deadline:
                assert total_timeout is not None  # noqa: S101
                raise _total_stall(total_timeout, chunk_idx) from e
            raise _stage_stall(stage_timeout, chunk_idx) from e
        assert isinstance(chunk, AIMessageChunk)  # noqa: S101
        # Arrival timestamps before fan-out: decode_ms measures the provider's
        # generation window (first token → last token), excluding the
        # synchronous SSE publish cost on our side.
        now = time.monotonic()
        if total_timeout is not None and now - started_at >= total_timeout:
            raise _total_stall(total_timeout, chunk_idx)
        if first_ts is None:
            first_ts = now
        last_ts = now
        chunks.append(chunk)
        chunk_idx += 1
        handler.process_chunk(chunk)


def _stage_stall(stage_timeout: float, chunk_idx: int) -> LLMStreamStallTimeoutError:
    stage_label = "TTFT" if chunk_idx == 0 else f"mid-stream after {chunk_idx} chunks"
    return LLMStreamStallTimeoutError(
        f"LLM stream stalled — no chunk for {stage_timeout:.1f}s ({stage_label}); "
        f"abort turn. Provider hang / network drop suspected.",
        stage="ttft" if chunk_idx == 0 else "mid-stream",
    )


def _total_stall(total_timeout: float, chunk_idx: int) -> LLMStreamStallTimeoutError:
    return LLMStreamStallTimeoutError(
        f"LLM stream exceeded {total_timeout:.1f}s total duration "
        f"after {chunk_idx} chunks; abort streaming attempt.",
        stage="total",
    )


def _next_timeout(
    stage_timeout: float, total_timeout: float | None, started_at: float, chunk_idx: int
) -> tuple[float, bool]:
    """`(timeout for the next chunk, whether the total ceiling is what bounds it)`."""
    if total_timeout is None:
        return stage_timeout, False
    remaining_total = total_timeout - (time.monotonic() - started_at)
    if remaining_total <= 0:
        raise _total_stall(total_timeout, chunk_idx)
    if remaining_total <= stage_timeout:
        return remaining_total, True
    return stage_timeout, False


async def _stream_llm(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    agent: AgentSlices,
    catalog: ModelCatalog,
) -> None:
    """Bind tools, stream the complete prefix and stamp latency/decode timing.

    Keep the existing SystemMessage byte-stable for provider implicit caching.
    Non-streaming fallback and empty streams have no decode window.
    """
    call_started = time.monotonic()
    runnable = llm.bind_tools([execute_code])
    first_ts, last_ts = await _consume_llm(
        runnable, messages, chunks=chunks, handler=handler, agent=agent, catalog=catalog
    )
    handler.llm_latency_ms = (time.monotonic() - call_started) * 1000.0
    handler.llm_decode_ms = (
        (last_ts - first_ts) * 1000.0 if first_ts is not None and last_ts is not None else None
    )
