"""Pending invocation state and the host's database-only failure boundary."""

from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from agent.impersonation import settle_checkpoint
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure, settle_turn_failure
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import AvaContext
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.runtime import TurnOutcome


@dataclass
class PendingWorkResult:
    """The completed invocation and its in-process database settlement phase."""

    result: dict[str, object]
    checkpoint_flushed: bool = False
    trace_attached: bool = False
    lifecycle_command_id: int | None = None


async def finish_pending_failure(
    graph: CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState],
    checkpointer: object,
    agent_id: int,
    ctx: AvaContext,
    config: RunnableConfig,
    pending: PendingTurnFailure,
    *,
    recovering: bool = False,
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
        await attach_trace_checkpoint_ref(graph, ctx, agent_id)
    return TurnOutcome(exited=False, crashed=True, aborted=True)
