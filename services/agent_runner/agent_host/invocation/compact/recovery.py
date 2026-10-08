"""Real serialized executor closure permits diagnostic uncertainty; never another generation."""

from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint
from base.agents.compaction.execution import CompactCommand, pending, require_receiver, settle
from base.agents.compaction.models import CompactHeldError, CompactMarker, CompactOutcome
from base.agents.history.checkpoint import latest_checkpoint_id_in_transaction
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources, bind_hosted_resources
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.invocation.compact.completion import close_terminal
from services.agent_runner.agent_host.native_work import (
    recover_native_cancel,
    settle_native_invocation,
)


async def settle_quiescent(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    agent_id: int,
    owner: UUID,
    resources: HostedTurnResources,
) -> None:
    """Called after the owned turn task exits and all resources close, before slot release.

    A fresh successor also reaches this boundary, but requires the public native
    receiver chain and exact typed resource admission. No status/lease expiry
    is evidence that an unknown original executor stopped.
    """
    if not _resources_closed(resources):
        raise CompactHeldError("compact original continuation is not quiescent")
    command = await pending(pool, agent_id)
    if command is None or command.attempt_id is None:
        return
    terminal = command.outcome in (CompactOutcome.APPLIED, CompactOutcome.REJECTED)
    if (
        command.result is not None
        and not terminal
        and command.outcome is not CompactOutcome.UNCERTAIN
    ):
        return
    if command.execution is None:
        raise CompactHeldError("unknown compact lacks original execution identity")
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT runtime_generation FROM agents_meta WHERE id=%s AND runtime_owner=%s "
                "AND runtime_kind='hosted' AND lease_expires_at>clock_timestamp()",
                (agent_id, owner),
            )
        ).fetchone()
    if row is None:
        raise CompactHeldError("compact executor has no current receiver")
    incarnation = RuntimeIncarnation(agent_id, row[0], owner)
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
    if terminal:
        with bind_hosted_resources(resources):
            await close_terminal(pool, saver, graph, incarnation, command)
        return
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    with bind_hosted_resources(resources):
        if (command.execution.generation, command.execution.owner) == (
            incarnation.generation,
            incarnation.owner,
        ):
            await settle_native_invocation(
                pool, saver, graph, incarnation, command.execution, config
            )
        else:
            await recover_native_cancel(pool, saver, graph, incarnation)
    checkpoint_id = await _pause_original(saver, graph, command, config)
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
        if await latest_checkpoint_id_in_transaction(conn, agent_id) != checkpoint_id:
            raise CompactHeldError("compact recovery pause is no longer the cold head")
        reason = await (
            await conn.execute(
                "SELECT reason FROM native_compact_commands WHERE id=%s FOR UPDATE",
                (command.acceptance.command_id,),
            )
        ).fetchone()
        if reason is None:
            raise CompactHeldError("compact recovery lost its original diagnostic record")
        await settle(
            conn,
            command,
            CompactOutcome.UNCERTAIN,
            reason=reason[0] or "generation_result_unknown",
            recovery_checkpoint_id=checkpoint_id,
            release=True,
        )
        await conn.execute(
            "UPDATE native_graph_work SET phase='settled',ended_at=now() WHERE id=%s",
            (command.execution.work_id,),
        )


async def _pause_original(
    saver: AsyncPostgresSaver, graph: CompactGraph, command: CompactCommand, config: RunnableConfig
) -> str:
    reader = cold_reader(saver)
    before = await reader.aget_tuple(config)
    if before is None:
        raise CompactHeldError("unknown compact has no cold checkpoint")
    if _has_application(before.checkpoint["channel_values"], command):
        raise CompactHeldError("unknown compact unexpectedly has an application marker")
    # Consumer pause is recovery projection, never a successful compact marker.
    await graph.aupdate_state(
        config,
        {
            "halted": True,
            "turn_active": False,
            "turn_idle": True,
            "native_work": command.execution,
        },
        as_node="claim",
    )
    await flush_checkpoint(saver, command.acceptance.target.source.agent_id)
    after = await reader.aget_tuple(config)
    if after is None:
        raise CompactHeldError("compact recovery pause is not persisted")
    values = after.checkpoint["channel_values"]
    if (
        values.get("halted") is not True
        or values.get("turn_idle") is not True
        or values.get("turn_active") is not False
        or NativeWorkTarget.model_validate(values.get("native_work")) != command.execution
        or _has_application(values, command)
    ):
        raise CompactHeldError("compact recovery has no cold no-application pause proof")
    return after.checkpoint["id"]


def _has_application(values: dict[str, Any], command: CompactCommand) -> bool:
    marker = values.get("native_compact")
    return (
        marker is not None and CompactMarker.model_validate(marker).acceptance == command.acceptance
    )


def _resources_closed(resources: HostedTurnResources) -> bool:
    return not resources.unresolved and all(task.done() for task in resources.completions)
