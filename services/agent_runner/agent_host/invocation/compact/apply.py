"""Apply only the stored original summary, then prove the materialized cold checkpoint."""

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.hooks.compact import build_compact_transition
from agent.impersonation import flush_checkpoint
from agent.nodes import END, INIT_CONTEXT
from agent.state import BaseAgentState, CompactState
from base.agents.compaction.application import authorize
from base.agents.compaction.execution import CompactCommand, require_receiver, settle
from base.agents.compaction.models import CompactHeldError, CompactOutcome
from base.agents.context import AvaContext
from base.agents.history.checkpoint import latest_checkpoint_id_in_transaction
from base.agents.messages.kwargs import AvaMsgType
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import hosted_resources_settled
from services.agent_runner.agent_host.invocation.compact.checkpoint import (
    cold_application,
    cold_reader,
    marker_for,
)

CompactGraph = CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]


async def acknowledge(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
) -> bool:
    checkpoint_id = await cold_application(saver, command)
    if checkpoint_id is None:
        return False
    if not hosted_resources_settled():
        raise CompactHeldError("compact application still has unresolved native resources")
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
        if await latest_checkpoint_id_in_transaction(conn, incarnation.agent_id) != checkpoint_id:
            raise CompactHeldError("compact application is no longer the persisted cold head")
        await settle(
            conn, command, CompactOutcome.APPLIED, checkpoint_id=checkpoint_id, release=False
        )
    return True


async def apply_prepared(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
    ctx: AvaContext,
) -> bool:
    """Intermediate reset is resumed without wiping/re-generating the result again."""
    if await acknowledge(pool, saver, command, incarnation):
        return True
    if not await authorize(pool, command, incarnation):
        return False
    marker = marker_for(command)
    reader = cold_reader(saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    cold = await reader.aget_tuple(config)
    if cold is None or command.result is None:
        raise CompactHeldError("compact source or durable result is absent")
    values = cold.checkpoint["channel_values"]
    existing = values.get("native_compact")
    if existing is None or type(marker).model_validate(existing) != marker:
        compact = CompactState.model_validate(values.get("compact", {}))
        if compact.version != command.acceptance.target.segment_version:
            raise CompactHeldError("compact segment changed after original application permit")
        result = command.result
        update = build_compact_transition(
            result.summary,
            resume=END,
            summary_kwargs={
                "id": str(result.message_id),
                "additional_kwargs": {
                    "ava_msg_type": AvaMsgType.COMPACT_REQUEST.value,
                    "ava_created_at": result.created_at.isoformat(),
                    "ava_compact_id": str(command.acceptance.command_id),
                },
            },
        )
        update.update(
            {
                "native_compact": marker,
                "native_work": command.execution,
                "native_cancel": None,
                "compact": compact.next_segment(),
                "halted": True,
                "turn_active": False,
                "turn_idle": True,
            }
        )
        await graph.aupdate_state(
            config, Command(update=update, goto=INIT_CONTEXT), as_node="claim"
        )
    # The shared INIT_CONTEXT owner materializes only the parked summary and
    # standing head; END prevents ordinary claim/model work before cold ACK.
    await graph.ainvoke(None, config=config, context=ctx)  # pyright: ignore[reportUnknownMemberType]
    await flush_checkpoint(saver, incarnation.agent_id)
    if not await acknowledge(pool, saver, command, incarnation):
        raise CompactHeldError(
            "compact replacement has no committed materialized application proof"
        )
    return True
