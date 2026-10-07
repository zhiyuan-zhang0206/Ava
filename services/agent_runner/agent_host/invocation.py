"""Pending invocation state and the host's database-only failure boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import settle_checkpoint
from agent.ownership.hosted_completion import completed_hosted_lifecycle_kind
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure, settle_turn_failure
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import AvaContext
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.native_process.runtime_incarnation import RuntimeIncarnation, current_incarnation
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.native_work import (
    completed_native_cancel,
    settle_native_invocation,
)
from services.agent_runner.agent_host.runtime import TurnOutcome


@dataclass
class PendingWorkResult:
    """The completed invocation and its in-process database settlement phase."""

    result: dict[str, object]
    checkpoint_flushed: bool = False
    trace_attached: bool = False
    lifecycle_command_id: int | None = None
    native_work: NativeWorkTarget | None = None
    native_settled: bool = False
    native_cancelled: bool = False


async def finish_pending_failure(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    checkpointer: object,
    agent_id: int,
    ctx: AvaContext,
    config: RunnableConfig,
    pending: PendingTurnFailure,
    *,
    recovering: bool = False,
    native_work: NativeWorkTarget | None = None,
) -> TurnOutcome:
    """Finish the original abort's durable writes without another invocation."""
    async with database_phase():
        if recovering:
            await settle_checkpoint(
                graph,
                ctx.require_db(),
                ctx.require_bus(),
                agent_id,
                ctx.relays,
                activate_accepted=False,
            )
        await settle_turn_failure(graph, checkpointer, config, ctx, agent_id, pending)
        if native_work is not None:
            incarnation = current_incarnation(agent_id)
            if (
                incarnation is None
                or ctx.ops_pool is None
                or not isinstance(checkpointer, AsyncPostgresSaver)
            ):
                raise RuntimeError(
                    "native failure settlement requires the original saver and incarnation"
                )
            await settle_native_invocation(
                ctx.ops_pool, checkpointer, graph, incarnation, native_work, config
            )
        await attach_trace_checkpoint_ref(graph, ctx, agent_id)
    return TurnOutcome(exited=False, crashed=True, aborted=True)


async def recover_completed_work(
    recover: Callable[[], Awaitable[None]],
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    pending: PendingWorkResult | None,
    native_work: NativeWorkTarget | None,
) -> str | None:
    """On ownership loss, accept only the original retained completion receipt."""
    try:
        await recover()
    except RuntimeOwnershipLostError:
        if (
            pending is not None
            and pending.checkpoint_flushed
            and pending.lifecycle_command_id is not None
        ):
            async with database_phase():
                kind = await completed_hosted_lifecycle_kind(
                    pool, incarnation, pending.lifecycle_command_id
                )
            if kind is not None:
                return kind
        if await completed_native_cancel(pool, native_work):
            return "native_cancel"
        raise
    return None
