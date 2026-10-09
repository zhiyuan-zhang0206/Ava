"""One retained hosted compact continuation, without regenerating unknown provider results."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.graph.interrupt import interruptible_model, subscribe_interrupt
from agent.hooks.compact import COMPACT_MIN_SUMMARY_CHARS, conversation_messages, generate_summary
from agent.hooks.compact_anchor import closing_of
from agent.state import AgentState
from base.agents.compaction.execution import (
    CompactCommand,
    claim_attempt,
    completed_original,
    history_unchanged,
    pending,
    require_receiver,
    save_result,
    settle,
)
from base.agents.compaction.models import CompactHeldError, CompactOutcome, PreparedSummary
from base.agents.compaction.source_owner import require_source_receiver
from base.agents.context import AvaContext
from base.agents.incarnation.native_work import activate_work
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.db.transaction import async_write_transaction
from base.lm.factory import build_chat_model_bound, provider_key_of_model
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph, apply_prepared
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.invocation.compact.completion import close_terminal
from services.agent_runner.agent_host.invocation.native_work import settle_native_invocation


@dataclass
class CompactContinuation:
    """Only this live continuation can retain an unstored provider result."""

    command: CompactCommand
    result: PreparedSummary | None
    generation_failed: bool = False


async def _settle_without_generation(
    pool: AsyncConnectionPool,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
    *,
    reason: str | None = None,
) -> None:
    async with async_write_transaction(pool) as conn:
        await require_source_receiver(conn, command.acceptance, incarnation)
        if reason is None and not await history_unchanged(conn, command):
            reason = "source_changed"
        await settle(
            conn,
            command,
            CompactOutcome.NOOP if reason is None else CompactOutcome.REJECTED,
            reason=reason,
        )


async def _generate_original(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
    ctx: AvaContext,
    continuation: CompactContinuation,
) -> bool:
    command = continuation.command
    reader = cold_reader(saver)
    cold = await reader.aget_tuple({"configurable": {"thread_id": str(incarnation.agent_id)}})
    if cold is None:
        raise CompactHeldError("compact source checkpoint is absent")
    state = AgentState.model_validate(cold.checkpoint["channel_values"])
    if not conversation_messages(state.messages):
        await _settle_without_generation(pool, command, incarnation)
        return False
    target_model = command.acceptance.target.model
    if ctx.require_agent().brain.llm_model != target_model:
        await _settle_without_generation(pool, command, incarnation, reason="model_changed")
        return False
    try:
        model, binding = build_chat_model_bound(
            target_model, overrides=ctx.require_agent().overrides, single_attempt=True
        )
    except ValueError:
        # Construction has not claimed an attempt or entered the provider.
        # Reject the now-unavailable contract instead of stranding its gate.
        await _settle_without_generation(
            pool, command, incarnation, reason="single_attempt_unavailable"
        )
        return False
    provider = provider_key_of_model(target_model)
    if provider is None:
        await _settle_without_generation(pool, command, incarnation, reason="provider_unavailable")
        return False
    command, claimed_here = await claim_attempt(pool, command, incarnation, provider_key=provider)
    continuation.command = command
    if command.outcome is CompactOutcome.REJECTED:
        return False
    if command.attempt_id is None or command.execution is None:
        raise CompactHeldError("compact original attempt was not bound")
    if not claimed_here:
        continuation.result = command.result
        return True
    async with async_write_transaction(pool) as conn:
        await activate_work(conn, command.execution)
    ctx = replace(ctx, native_work=command.execution)
    try:
        async with subscribe_interrupt(
            pool, incarnation.agent_id, incarnation=ctx.original_incarnation, work=ctx.native_work
        ) as interrupted:
            summary = await interruptible_model(
                generate_summary(
                    list(state.messages),
                    model,
                    ctx.require_agent(),
                    single_attempt=True,
                    binding=binding,
                ),
                interrupted,
            )
        continuation.result = PreparedSummary(
            attempt_id=command.attempt_id,
            summary=summary,
            message_id=uuid4(),
            created_at=datetime.now(UTC),
            closing=closing_of(summary),
        )
    except BaseException:
        continuation.generation_failed = True
        raise
    return True


async def _mark_unknown(
    pool: AsyncConnectionPool,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
) -> None:
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
        await settle(
            conn,
            command,
            CompactOutcome.UNCERTAIN,
            reason="generation_result_unknown",
            release=False,
        )


async def _finish_original(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    incarnation: RuntimeIncarnation,
    ctx: AvaContext,
    continuation: CompactContinuation,
) -> bool:
    command = continuation.command
    if command.execution is None:
        raise CompactHeldError("compact continuation lacks its original execution")
    ctx = replace(ctx, native_work=command.execution)
    if continuation.result is None:
        await _mark_unknown(pool, command, incarnation)
        return False
    command = await save_result(pool, command, incarnation, continuation.result)
    continuation.command = command
    if command.outcome in (CompactOutcome.APPLIED, CompactOutcome.REJECTED):
        return await close_terminal(
            pool, saver, graph, incarnation, command, resources=ctx.hosted_resources
        )
    if len(continuation.result.summary) < COMPACT_MIN_SUMMARY_CHARS:
        async with async_write_transaction(pool) as conn:
            await require_receiver(conn, command, incarnation)
            await settle(
                conn, command, CompactOutcome.REJECTED, reason="summary_unusable", release=False
            )
    else:
        await apply_prepared(pool, saver, graph, command, incarnation, ctx)
    current = await pending(pool, incarnation.agent_id)
    if current is None:
        raise CompactHeldError("compact lost its original native closure pointer")
    if current.outcome in (CompactOutcome.APPLIED, CompactOutcome.REJECTED):
        return await close_terminal(
            pool, saver, graph, incarnation, current, resources=ctx.hosted_resources
        )
    return False


async def run_compact(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    incarnation: RuntimeIncarnation,
    ctx: AvaContext,
    recover: Callable[[NativeWorkTarget | None], Awaitable[None]],
) -> bool:
    """Return whether ordinary work may start; uncertainty retains the original gate."""
    command = await pending(pool, incarnation.agent_id)
    if command is None:
        return True
    continuation = CompactContinuation(command, command.result)
    if command.outcome is CompactOutcome.UNCERTAIN:
        return False
    while True:
        try:
            if await completed_original(pool, continuation.command):
                if continuation.command.execution is not None:
                    await settle_native_invocation(
                        pool,
                        saver,
                        graph,
                        incarnation,
                        continuation.command.execution,
                        {"configurable": {"thread_id": str(incarnation.agent_id)}},
                        resources=ctx.hosted_resources,
                    )
                return True
            if continuation.command.attempt_id is None and not await _generate_original(
                pool, saver, incarnation, ctx, continuation
            ):
                return True
            return await _finish_original(pool, saver, graph, incarnation, ctx, continuation)
        except (psycopg.OperationalError, PoolTimeout):
            # Preserve this exact local result and UUID while the database recovers.
            await recover(continuation.command.execution)
            if continuation.generation_failed and continuation.command.attempt_id is not None:
                await _mark_unknown(pool, continuation.command, incarnation)
                return False
        except Exception:
            if continuation.generation_failed and continuation.command.attempt_id is not None:
                await _mark_unknown(pool, continuation.command, incarnation)
                return False
            raise
