"""One streaming model request under stage and total deadlines.

Provider transport failures are normalized at the call boundary. Stall,
protocol and permanent failures propagate without a fallback request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm_errors import (
    LLMStreamStallTimeoutError,
)
from agent.llm import execute_code
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.errors import normalize_provider_transport_error


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
    """Stream once under owned deadlines; any failure propagates to the node."""
    from base.lm.registry import resolve_setting

    model = agent.brain.llm_model
    return await _consume_stream_with_stall_timeout(
        bound_llm.astream(messages).__aiter__(),  # type: ignore[attr-defined]
        chunks=chunks,
        handler=handler,
        ttft_timeout=resolve_setting(
            "llm_stream_ttft_timeout_seconds",
            model=model,
            models=catalog.models,
            explicit=agent.overrides.llm_stream_ttft_timeout_seconds,
        ),
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
    Empty streams have no decode window.
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
