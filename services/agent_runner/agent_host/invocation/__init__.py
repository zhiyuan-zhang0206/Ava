"""Pending invocation state and the host's database-only failure boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint, settle_checkpoint
from agent.ownership.hosted_completion import (
    completed_hosted_lifecycle_kind,
    pending_hosted_lifecycle_id,
)
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure, settle_turn_failure
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import AvaContext
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.messages.native_restart import original_guarded_restart_id
from base.agents.observation.relay_supervision import RelaySupervision
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.invocation.checkpoints import TurnCheckpoints
from services.agent_runner.agent_host.invocation.compact.lifecycle import apply_hosted_lifecycle
from services.agent_runner.agent_host.invocation.native_work import (
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
                incarnation=ctx.original_incarnation,
                resources=ctx.hosted_resources,
            )
        await settle_turn_failure(graph, checkpointer, config, ctx, agent_id, pending)
        if native_work is not None:
            incarnation = ctx.require_original_incarnation(agent_id)
            if ctx.ops_pool is None or not isinstance(checkpointer, AsyncPostgresSaver):
                raise RuntimeError(
                    "native failure settlement requires the original saver and incarnation"
                )
            await settle_native_invocation(
                ctx.ops_pool,
                checkpointer,
                graph,
                incarnation,
                native_work,
                config,
                resources=ctx.hosted_resources,
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


async def returned_lifecycle_request(
    pool: AsyncConnectionPool,
    agent_id: int,
    pending: PendingWorkResult,
    *,
    incarnation: RuntimeIncarnation | None,
) -> bool:
    """An exact guarded receipt may survive a cancel return with legacy flags clear."""
    if not pending.checkpoint_flushed or not pending.native_settled:
        raise RuntimeError("returned lifecycle selection requires durable work settlement")
    if pending.lifecycle_command_id is None and pending.native_work is not None:
        async with pool.connection() as conn:
            pending.lifecycle_command_id = await original_guarded_restart_id(
                conn, pending.native_work
            )
    requested = bool(pending.result["exit_requested"] or pending.result["restart_requested"])
    if pending.lifecycle_command_id is None and requested:
        if incarnation is not None:
            incarnation.require_agent(agent_id)
        if incarnation is None:
            raise RuntimeError("hosted lifecycle return has no admitted incarnation")
        pending.lifecycle_command_id = await pending_hosted_lifecycle_id(pool, incarnation)
    return pending.lifecycle_command_id is not None or requested


async def finish_completed_invocation(
    pool: AsyncConnectionPool,
    checkpoints: TurnCheckpoints,
    agent_id: int,
    ctx: AvaContext,
    pending: PendingWorkResult,
    drop_agent: Callable[[int], None],
    kill_shell_sessions: Callable[[int], None],
    *,
    db: Database,
    bus: EventBus,
    relays: RelaySupervision,
) -> TurnOutcome | None:
    """Settle the original completed work through its admission's checkpoint view."""
    # Correlate the original trace only after its checkpoint is durable.
    async with database_phase():
        if not pending.checkpoint_flushed:
            await flush_checkpoint(checkpoints.saver, agent_id)
            pending.checkpoint_flushed = True
        if not pending.native_settled:
            incarnation = ctx.require_original_incarnation(agent_id)
            pending.native_cancelled = await settle_native_invocation(
                pool,
                checkpoints.saver,
                checkpoints.graph,
                incarnation,
                pending.native_work,
                {"configurable": {"thread_id": str(agent_id)}},
                resources=ctx.hosted_resources,
            )
            pending.native_settled = True
        if not pending.trace_attached:
            await attach_trace_checkpoint_ref(checkpoints.graph, ctx, agent_id)
            pending.trace_attached = True
    if await returned_lifecycle_request(
        pool, agent_id, pending, incarnation=ctx.original_incarnation
    ):
        incarnation = ctx.require_original_incarnation(agent_id)
        drop_agent(agent_id)
        async with database_phase():
            if pending.lifecycle_command_id is None:
                return TurnOutcome(exited=False, crashed=False)
            kind = await apply_hosted_lifecycle(
                pool,
                incarnation,
                bus=bus,
                kill_shell_sessions=kill_shell_sessions,
                expected_command_id=pending.lifecycle_command_id,
                resources=ctx.hosted_resources,
            )
            if kind is None:
                kind = await completed_hosted_lifecycle_kind(
                    pool, incarnation, pending.lifecycle_command_id
                )
        logger.info(
            "hosted lifecycle return settled",
            agent_id=agent_id,
            generation=str(incarnation.generation),
            command_kind=kind,
        )
        return TurnOutcome(exited=kind == "terminate", crashed=False)
    if pending.native_cancelled or pending.result["turn_idle"]:
        async with database_phase():
            await settle_checkpoint(
                checkpoints.graph,
                db,
                bus,
                agent_id,
                relays,
                incarnation=ctx.original_incarnation,
                resources=ctx.hosted_resources,
            )
        return TurnOutcome(exited=False, crashed=False)
    return None
