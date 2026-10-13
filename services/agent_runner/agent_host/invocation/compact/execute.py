"""One retained hosted compact continuation, without regenerating unknown provider results."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks.compact import (
    COMPACT_MIN_SUMMARY_CHARS,
    conversation_messages,
    generate_summary,
)
from agent.graph.interrupt import interruptible_model, subscribe_interrupt
from agent.hooks.compact_anchor import closing_of
from agent.state import AgentState
from base.agents.compaction.execution import (
    CompactCommand,
    claim_attempt,
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
from base.config import settings
from base.db.transaction import async_write_transaction
from base.lm.factory import build_chat_model_bound, provider_key_of_model
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph, apply_prepared
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.invocation.compact.completion import close_terminal


@dataclass
class CompactContinuation:
    """Only this live continuation can retain an unstored provider result."""

    command: CompactCommand
    result: PreparedSummary | None


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
        model, _binding = build_chat_model_bound(
            target_model,
            overrides=ctx.require_agent().overrides,
            single_attempt=True,
            catalog=ctx.require_catalog(),
            llm_override=settings.lm.llm_override,
        )
    except ValueError:
        # Construction has not claimed an attempt or entered the provider.
        # Reject the now-unavailable contract instead of stranding its gate.
        await _settle_without_generation(
            pool, command, incarnation, reason="single_attempt_unavailable"
        )
        return False
    provider = provider_key_of_model(target_model, catalog=ctx.require_catalog())
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
    async with subscribe_interrupt(
        pool,
        incarnation.agent_id,
        incarnation=ctx.original_incarnation,
        work=ctx.native_work,
        resources=ctx.hosted_resources,
    ) as interrupted:
        summary = await interruptible_model(
            generate_summary(
                list(state.messages),
                model,
                ctx.require_agent(),
                catalog=ctx.require_catalog(),
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
) -> bool:
    """Run a fresh command once; an interrupted attempt stays held for inspection."""
    command = await pending(pool, incarnation.agent_id)
    if command is None:
        return True
    if command.attempt_id is not None or command.outcome is CompactOutcome.UNCERTAIN:
        logger.warning(
            "interrupted compact is not automatically resumed", agent_id=incarnation.agent_id
        )
        return False
    continuation = CompactContinuation(command, command.result)
    try:
        if not await _generate_original(pool, saver, incarnation, ctx, continuation):
            return True
        return await _finish_original(pool, saver, graph, incarnation, ctx, continuation)
    except Exception:
        if continuation.command.attempt_id is not None:
            try:
                await _mark_unknown(pool, continuation.command, incarnation)
            except Exception:
                logger.opt(exception=True).warning(
                    "compact failure could not be persisted", agent_id=incarnation.agent_id
                )
        raise
