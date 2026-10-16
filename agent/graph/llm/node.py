from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph._callbacks import RedisStreamHandler
from agent.graph.hooks.compact import auto_compact_for_llm
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
    LlmLedger,
    LLMRetryBudgetExceededError,
)
from agent.graph.node_log import node_lifecycle
from agent.graph.tool_calls import code_from_args
from agent.hooks.understanding_chunks import due_chunk_update
from agent.llm.usage import log_llm_usage
from agent.nodes import AFTER_EXEC, BEFORE_EXEC
from agent.state_channels import CircuitState
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.messages.kwargs import read_ava_kwargs
from base.db.transaction import async_write_transaction
from base.events.live.projection import TokenUsage
from base.events.live.publisher import AgentEventPublisher
from base.lm.catalog import ModelCatalog
from base.lm.reasoning import content_blocks
from base.log import logger

from ._cancel import race_stream_vs_cancel
from ._chunk import assemble_final_message
from ._retry import RETRY_REMAINING_ATTR, Attempt, retry_wait
from ._stream import stream_llm
from .goto import LlmGoto


def _log_llm_retry_duration(
    attempt: Attempt,
    *,
    outcome: Literal["succeeded", "attempts_exhausted", "budget_exhausted"],
) -> None:
    if attempt.number < 2:
        return
    logger.info(
        "LLM retry sequence {outcome} after {duration_seconds:.2f}s",
        event="llm_retry",
        outcome=outcome,
        duration_seconds=attempt.elapsed_seconds(),
    )


def _raise_retry_budget_exhausted(attempt: int, *, read_lm: Callable[[str], Any]) -> NoReturn:
    raise LLMRetryBudgetExceededError(
        "LLM retry wall-clock budget "
        f"({read_lm('llm_retry_max_total_seconds'):.0f}s) exhausted before attempt {attempt}"
    )


def _finalize_turn_observability(
    publisher: AgentEventPublisher,
    agent_id: int,
    final_msg: AIMessage,
    handler: RedisStreamHandler,
    model: str,
    *,
    catalog: ModelCatalog,
) -> None:
    kw = read_ava_kwargs(final_msg)
    kw["ava_created_at"] = datetime.now(UTC).isoformat()
    reasoning_ms_by_block = handler.reasoning_ms_by_block
    if reasoning_ms_by_block:
        kw["ava_reasoning_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in reasoning_ms_by_block.items()
        }
    code_ms_by_block = handler.code_ms_by_block
    if code_ms_by_block:
        kw["ava_code_ms_by_block"] = {
            str(block_idx): ms for block_idx, ms in code_ms_by_block.items()
        }
    log_llm_usage(
        final_msg,
        model=model,
        agent_id=agent_id,
        catalog=catalog,
        latency_ms=handler.llm_latency_ms,
        decode_ms=handler.llm_decode_ms,
    )
    usage = final_msg.usage_metadata or {}
    from base.lm.reasoning import extract_reasoning_tokens

    reasoning_tokens = extract_reasoning_tokens(
        final_msg.usage_metadata,
        total_reasoning_chars=handler.total_reasoning_chars,
    )
    publisher.emit(
        TokenUsage(
            agent_id=agent_id,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            reasoning_tokens=reasoning_tokens,
        ).model_dump_json()
    )


async def llm_node(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
    *,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    agent_id = agent_id_from_config(config)
    model = runtime.context.require_agent().brain.llm_model
    first_started_at = time.time()
    failed = 0
    while True:
        try:
            return await llm_attempt(
                state, runtime, config, Attempt(failed + 1, first_started_at), ledger
            )
        except GraphBubbleUp:
            raise
        except Exception as exc:
            failed += 1
            wait = retry_wait(
                exc,
                failed,
                model=model,
                agent_id=agent_id,
                catalog=runtime.context.require_catalog(),
                read_lm=lambda field: runtime.context.require_agent().read("lm", field),
                max_attempts_pin=runtime.context.require_agent().read(
                    "lm", "llm_retry_max_attempts"
                ),
            )
            if wait is None:
                raise
            await asyncio.sleep(wait)
            logger.info(
                "Retrying the llm node after {wait:.2f}s (failed try {failed}): {error}",
                wait=wait,
                failed=failed,
                error=f"{type(exc).__name__}: {exc}",
            )


def _enforce_retry_budget(attempt: Attempt, *, read_lm: Callable[[str], Any]) -> None:
    if attempt.elapsed_seconds() >= read_lm("llm_retry_max_total_seconds"):
        _log_llm_retry_duration(attempt, outcome="budget_exhausted")
        _raise_retry_budget_exhausted(attempt.number, read_lm=read_lm)


def _note_failed_attempt(
    exc: BaseException, attempt: Attempt, runtime: Runtime[AvaContext], agent_id: int
) -> None:
    runtime.context.turn_progress.mark(agent_id)
    if isinstance(exc, Exception):
        remaining_seconds = (
            runtime.context.require_agent().read("lm", "llm_retry_max_total_seconds")
            - attempt.elapsed_seconds()
        )
        if remaining_seconds <= 0.0 and not isinstance(exc, LLMRetryBudgetExceededError):
            _log_llm_retry_duration(attempt, outcome="budget_exhausted")
        elif not isinstance(exc, (FatalLLMStreamError, FatalProviderError)):
            from base.lm.registry import resolve_setting

            max_attempts = resolve_setting(
                "llm_retry_max_attempts",
                model=runtime.context.require_agent().brain.llm_model,
                models=runtime.context.require_catalog().models,
                explicit=runtime.context.require_agent().read("lm", "llm_retry_max_attempts"),
            )
            if attempt.number >= max_attempts:
                _log_llm_retry_duration(attempt, outcome="attempts_exhausted")
        with contextlib.suppress(AttributeError):
            setattr(exc, RETRY_REMAINING_ATTR, remaining_seconds)


# ava:ref agent/graph/llm/docs/turn-records.ava.okf.md#turn-end-owner
async def llm_attempt(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
    attempt: Attempt,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    turn_start = time.monotonic()
    agent_id = agent_id_from_config(config)
    event_publisher = runtime.context.event_publisher
    if event_publisher is None:
        raise RuntimeError("llm_node requires ctx.event_publisher")
    async with node_lifecycle(
        "llm",
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=runtime.context.turn_progress,
        read_stall_seconds=lambda: runtime.context.require_agent().read(
            "agent", "node_stall_dump_seconds"
        ),
        timeline_inputs=TimelineReadInputs(
            runtime.context.require_clock,
            lambda: runtime.context.require_agent().read("general", "message_timestamps"),
        ),
        limit_reader=lambda: runtime.context.require_agent().read(
            "display", "timeline_default_limit"
        ),
    ):
        try:
            _enforce_retry_budget(
                attempt,
                read_lm=lambda field: runtime.context.require_agent().read("lm", field),
            )
            result = await _llm_node_impl(state, runtime, config, ledger)
        except BaseException as exc:
            _note_failed_attempt(exc, attempt, runtime, agent_id)
            logger.opt(exception=True).warning(
                "turn ended in {duration_seconds:.2f}s ok=False — _llm_node_impl raised",
                event="turn_end",
                agent_id=agent_id,
                duration_seconds=time.monotonic() - turn_start,
                ok=False,
            )
            raise
        else:
            _log_llm_retry_duration(attempt, outcome="succeeded")
            logger.info(
                "turn ended in {duration_seconds:.2f}s ok=True",
                event="turn_end",
                agent_id=agent_id,
                duration_seconds=time.monotonic() - turn_start,
                ok=True,
            )
            update = result.update
            if state.circuit.open and update is not None and "messages" in update:
                update["circuit"] = CircuitState()
                logger.info(
                    "heartbeat circuit breaker CLOSED — LLM call accepted again",
                    event="circuit_breaker_closed",
                    agent_id=agent_id_from_config(config),
                )
            return result


def _is_silent_idle(final_msg: AIMessage) -> bool:
    if final_msg.tool_calls or final_msg.text:
        return False
    output_tokens = (final_msg.usage_metadata or {}).get("output_tokens", 0)
    _content: Any = final_msg.content
    has_thinking_block = isinstance(_content, list) and any(
        isinstance(b, dict) and cast(dict[str, Any], b).get("type") == "thinking"
        for b in content_blocks(cast(list[Any], _content))
    )
    return (
        has_thinking_block
        or bool(final_msg.additional_kwargs.get("reasoning_content"))
        or output_tokens > 0
    )


def _silent_idle_command(
    final_msg: AIMessage,
    agent_id: int,
    model: str,
    ledger: LlmLedger,
    *,
    catalog: ModelCatalog,
    read_lm: Callable[[str], Any],
) -> Command[LlmGoto] | None:
    if not _is_silent_idle(final_msg):
        return None
    tid = str(agent_id)
    usage = final_msg.usage_metadata or {}
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    budget_tokens = max(output_tokens, 1)
    cumulative_output_tokens = ledger.silent_idle_output_tokens(tid) + budget_tokens
    cap = read_lm("llm_silent_idle_max_output_tokens")
    from base.lm.pricing import quote

    priced = quote(model, 0, output_tokens, 0, prices=catalog.prices)
    estimated_cost_usd = priced.cost_usd if priced is not None else None
    if cap > 0 and cumulative_output_tokens >= cap:
        ledger.reset_silent_idle(tid)
        logger.warning(
            "[{label}] {body}",
            label="silent-idle",
            event="silent_idle",
            body=(
                f"reasoning-only output reached {cumulative_output_tokens} budget tokens (cap {cap}) — "
                "halting to idle instead of looping"
            ),
            output_tokens=output_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            halted=True,
        )
        return Command[LlmGoto](
            update={"messages": [final_msg], "halted": True},
            goto=AFTER_EXEC,
        )
    ledger.record_silent_idle_output_tokens(tid, cumulative_output_tokens)
    logger.info(
        "[{label}] {body}",
        label="silent-idle",
        event="silent_idle",
        body=(
            f"reasoning-only output {output_tokens} tokens "
            f"(budget {budget_tokens}; cumulative {cumulative_output_tokens}/{cap or 'inf'}) "
            "— continue-loop with nudge"
        ),
        output_tokens=output_tokens,
        cumulative_output_tokens=cumulative_output_tokens,
        estimated_cost_usd=estimated_cost_usd,
        halted=False,
    )
    return Command[LlmGoto](
        update={"messages": [final_msg], "halted": False},
        goto=AFTER_EXEC,
    )


async def persist_last_active(ctx: AvaContext, agent_id: int, text: str) -> None:
    ctx.turn_progress.mark(agent_id)
    if ctx.ops_pool is None:
        return
    try:
        async with async_write_transaction(ctx.ops_pool) as conn, conn.cursor() as cur:
            if text:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_message_text = %s, "
                    "last_turn_fatal_at = NULL, permanent_reject_streak = 0, last_permanent_reject_reason = NULL "
                    "WHERE id = %s",
                    (text, agent_id),
                )
            else:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_turn_fatal_at = NULL, "
                    "permanent_reject_streak = 0, last_permanent_reject_reason = NULL "
                    "WHERE id = %s",
                    (agent_id,),
                )
    except Exception:
        logger.warning(
            "[{label}] {body}",
            label="last-msg",
            event="last_msg",
            body=f"failed to persist last_active_at / last_message_text for agent {agent_id}",
        )


async def _llm_node_impl(
    state: _state.AgentState,
    runtime: Runtime[AvaContext],
    config: RunnableConfig,
    ledger: LlmLedger,
) -> Command[LlmGoto]:
    ctx = runtime.context
    if ctx.event_publisher is None:
        raise RuntimeError("_llm_node_impl requires ctx.event_publisher")
    if ctx.llm is None:
        raise RuntimeError("_llm_node_impl requires ctx.llm")
    publisher = ctx.event_publisher
    llm = ctx.llm
    agent_id = agent_id_from_config(config)

    compacted = await auto_compact_for_llm(state, runtime, config)
    if compacted is not None:
        goto = compacted.pop("goto")
        return Command[LlmGoto](update=compacted, goto=goto)

    handler = RedisStreamHandler(
        publisher,
        agent_id,
        msg_idx=len(state.messages),
        turn_progress=ctx.turn_progress,
    )

    chunks: list[AIMessageChunk] = []

    cancelled_cmd = await race_stream_vs_cancel(
        ctx,
        agent_id,
        stream_llm(
            llm,
            list(state.messages),
            chunks=chunks,
            handler=handler,
            agent=ctx.require_agent(),
            catalog=ctx.require_catalog(),
        ),
        handler,
    )
    if cancelled_cmd is not None:
        return cancelled_cmd

    if not chunks:
        return Command[LlmGoto](update={"messages": [AIMessage(content="")]}, goto=BEFORE_EXEC)
    final_msg = assemble_final_message(chunks, catalog=ctx.require_catalog())

    text = final_msg.text
    if text:
        logger.info("[text] {body}", event="text", body=text)
    await persist_last_active(ctx, agent_id, text)
    _finalize_turn_observability(
        publisher,
        agent_id,
        final_msg,
        handler,
        ctx.require_agent().brain.llm_model,
        catalog=ctx.require_catalog(),
    )

    cut_update = await due_chunk_update(
        state.compact,
        list(state.messages),
        final_msg,
        read_agent=lambda field: ctx.require_agent().read("agent", field),
        pool=ctx.ops_pool,
        agent_id=agent_id,
        model=ctx.require_agent().brain.llm_model,
        overrides=ctx.require_agent().overrides,
        catalog=ctx.require_catalog(),
    )

    silent_idle_cmd = _silent_idle_command(
        final_msg,
        agent_id,
        ctx.require_agent().brain.llm_model,
        ledger,
        catalog=ctx.require_catalog(),
        read_lm=lambda field: ctx.require_agent().read("lm", field),
    )
    if silent_idle_cmd is not None:
        return Command[LlmGoto](
            update={**cast("dict[str, Any]", silent_idle_cmd.update), **cut_update},
            goto=silent_idle_cmd.goto,
        )

    ledger.reset_silent_idle(str(agent_id))

    if not final_msg.tool_calls:
        if not text:
            logger.warning(
                "[halt] {body}",
                event="halt",
                body="no tool_call and EMPTY text — empty turn, user sees no reply",
            )
        else:
            logger.info("[halt] {body}", event="halt", body="no tool_call (idle)")
        return Command[LlmGoto](
            update={"messages": [final_msg], "halted": True, **cut_update},
            goto=AFTER_EXEC,
        )
    logger.info(
        "[code] {body}",
        event="code",
        body=code_from_args(final_msg.tool_calls[0]["args"], source="llm final_msg tool_call"),
    )
    return Command[LlmGoto](update={"messages": [final_msg], **cut_update}, goto=BEFORE_EXEC)
