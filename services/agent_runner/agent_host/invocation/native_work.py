"""Original native work settlement and cold cancel recovery; no graph replay."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from agent.impersonation import flush_checkpoint, settle_checkpoint
from agent.ownership.native_cancel import halt_for_native_cancel
from agent.state import BaseAgentState
from base.agents.context import AvaContext
from base.agents.history.checkpoint import CheckpointReadError
from base.agents.impersonation.notes import HandoffNotes
from base.agents.incarnation.native_work import load_work, prepare_work
from base.agents.incarnation.native_work_models import (
    NativeCancelAcceptance,
    NativeCancelMarker,
    NativeCancelOutcome,
    NativeWorkTarget,
    NativeWorkUncertainError,
)
from base.agents.messages.native_cancel import finish_native_cancel, require_cancel_receiver
from base.agents.observation.relay_supervision import RelaySupervision
from base.db import Database
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources, hosted_resources_settled
from services.agent_runner.agent_host.db_recovery import database_phase

_Graph = CompiledStateGraph[BaseAgentState, AvaContext, BaseAgentState, BaseAgentState]


@dataclass
class NativeWorkContinuation:
    """One UUID survives preparation, graph return and every DB recovery retry."""

    work_id: UUID
    target: NativeWorkTarget | None = None
    begun: bool = False


async def prepare_native_invocation(
    pool: AsyncConnectionPool,
    work: NativeWorkContinuation,
    incarnation: RuntimeIncarnation,
) -> None:
    if work.begun:
        return
    async with async_write_transaction(pool) as conn:
        row = await (
            await conn.execute(
                "SELECT machine FROM agents_meta WHERE id=%s AND runtime_generation=%s "
                "AND runtime_owner=%s FOR UPDATE",
                (incarnation.agent_id, incarnation.generation, incarnation.owner),
            )
        ).fetchone()
        if row is None:
            raise NativeWorkUncertainError("native preparation lost its admitted owner")
        target = NativeWorkTarget(
            work_id=work.work_id,
            agent_id=incarnation.agent_id,
            machine=row[0],
            generation=incarnation.generation,
            owner=incarnation.owner,
            protocol=1,
        )
        work.target = await prepare_work(conn, target)
    work.begun = True


async def command_for_work(
    pool: AsyncConnectionPool, target: NativeWorkTarget
) -> tuple[NativeCancelMarker, NativeCancelOutcome] | None:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome FROM native_cancel_commands WHERE work_id=%s",
                (target.work_id,),
            )
        ).fetchone()
    if row is None:
        return None
    accepted = NativeCancelAcceptance.model_validate(row[0])
    if accepted.target != target:
        raise NativeWorkUncertainError("original native cancel identity changed")
    return NativeCancelMarker(command_id=accepted.command_id, target=target), NativeCancelOutcome(
        row[1]
    )


async def cold_cancel_checkpoint(
    saver: AsyncPostgresSaver, marker: NativeCancelMarker
) -> str | None:
    """Read the latest persisted channels, excluding pending writes/graph state."""
    # A fresh source saver bypasses runtime reconstruction caches and pending
    # buffer state. This proof needs the committed channels, never old messages.
    reader = AsyncPostgresSaver(saver.conn, serde=saver.serde)
    tuple_ = await reader.aget_tuple({"configurable": {"thread_id": str(marker.target.agent_id)}})
    if tuple_ is None:
        return None
    values = tuple_.checkpoint["channel_values"]
    if (
        values.get("halted") is not True
        or values.get("native_work") is None
        or values.get("native_cancel") is None
    ):
        return None
    try:
        target = NativeWorkTarget.model_validate(values["native_work"])
        application = NativeCancelMarker.model_validate(values["native_cancel"])
    except ValidationError as exc:
        raise NativeWorkUncertainError("native checkpoint marker is malformed") from exc
    if target != marker.target or application != marker:
        return None
    return tuple_.checkpoint["id"]


async def _close_without_cancel(
    pool: AsyncConnectionPool, target: NativeWorkTarget
) -> tuple[NativeCancelMarker, NativeCancelOutcome] | None:
    async with async_write_transaction(pool) as conn:
        await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (target.agent_id,))
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome FROM native_cancel_commands WHERE work_id=%s",
                (target.work_id,),
            )
        ).fetchone()
        if row is not None:
            accepted = NativeCancelAcceptance.model_validate(row[0])
            if accepted.target != target:
                raise NativeWorkUncertainError("original native work target changed")
            return NativeCancelMarker(
                command_id=accepted.command_id, target=target
            ), NativeCancelOutcome(row[1])
        # No protected command: bookkeeping must not impose a new legacy startup hold.
        await conn.execute(
            "UPDATE native_graph_work SET phase='settled',ended_at=now() WHERE id=%s "
            "AND phase IN ('preparing','active','uncertain')",
            (target.work_id,),
        )
    return None


async def settle_native_invocation(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: _Graph,
    incarnation: RuntimeIncarnation,
    target: NativeWorkTarget | None,
    config: RunnableConfig,
    *,
    resources: HostedTurnResources | None,
) -> bool:
    """Settle only the original returned/unwound work before claiming another."""
    if target is None:
        return False
    command = await _close_without_cancel(pool, target)
    if command is None:
        return False
    marker, outcome = command
    if outcome in (NativeCancelOutcome.APPLIED, NativeCancelOutcome.RECOVERED_STOPPED):
        return True
    if not hosted_resources_settled(resources):
        raise NativeWorkUncertainError("native continuation has unresolved owned resources")
    checkpoint_id = await cold_cancel_checkpoint(saver, marker)
    if checkpoint_id is None:
        async with async_write_transaction(pool) as conn:
            await conn.execute(
                "SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (target.agent_id,)
            )
            stored = await load_work(conn, target.work_id, lock=True)
            if stored is None or stored.target != target:
                raise NativeWorkUncertainError("native work original evidence is missing")
            await require_cancel_receiver(conn, stored, incarnation)
        await graph.aupdate_state(
            config, {**halt_for_native_cancel(marker), "native_work": target}, as_node="claim"
        )
        await flush_checkpoint(saver, target.agent_id)
        checkpoint_id = await cold_cancel_checkpoint(saver, marker)
    if checkpoint_id is None or not hosted_resources_settled(resources):
        raise NativeWorkUncertainError("native halt checkpoint has not durably settled")
    async with async_write_transaction(pool) as conn:
        await finish_native_cancel(
            conn,
            incarnation,
            marker,
            outcome=NativeCancelOutcome.APPLIED,
            checkpoint_id=checkpoint_id,
        )
    return True


async def _mark_uncertain(pool: AsyncConnectionPool, marker: NativeCancelMarker) -> None:
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (marker.target.agent_id,)
        )
        cursor = await conn.execute(
            "UPDATE native_cancel_commands SET outcome='uncertain' WHERE id=%s AND work_id=%s "
            "AND outcome IN ('accepted','uncertain') RETURNING id",
            (marker.command_id, marker.target.work_id),
        )
        if await cursor.fetchone() is not None:
            await conn.execute(
                "UPDATE native_graph_work SET phase='uncertain' WHERE id=%s",
                (marker.target.work_id,),
            )


async def _pause_certified_successor(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: _Graph,
    incarnation: RuntimeIncarnation,
    marker: NativeCancelMarker,
) -> str:
    """Pause the receiver's old conversation; never fabricate original execution.

    A certified replacement cannot overlap this continuation: same-host force
    observation is produced only after the serialized pump drains it; exact
    predecessor process exit prevents that predecessor from writing again.
    """
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (incarnation.agent_id,)
        )
        work = await load_work(conn, marker.target.work_id, lock=True)
        if work is None or work.target != marker.target or not work.transfers:
            raise NativeWorkUncertainError("native recovery lacks certified predecessor stop")
        await require_cancel_receiver(conn, work, incarnation)
    config: RunnableConfig = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    await graph.aupdate_state(
        config,
        {
            "halted": True,
            "turn_active": False,
            "turn_idle": True,
            "native_work": marker.target,
            "native_cancel": None,
        },
        as_node="claim",
    )
    await flush_checkpoint(saver, incarnation.agent_id)
    reader = AsyncPostgresSaver(saver.conn, serde=saver.serde)
    tuple_ = await reader.aget_tuple(config)
    if tuple_ is None:
        raise NativeWorkUncertainError("native recovery pause checkpoint is missing")
    values = tuple_.checkpoint["channel_values"]
    if (
        values.get("halted") is not True
        or values.get("turn_idle") is not True
        or values.get("turn_active") is not False
        or values.get("native_cancel") is not None
        or NativeWorkTarget.model_validate(values.get("native_work")) != marker.target
    ):
        raise NativeWorkUncertainError(
            "native recovery pause checkpoint does not match original work"
        )
    return tuple_.checkpoint["id"]


def _require_settled_continuation(*, resources: HostedTurnResources | None) -> None:
    if not hosted_resources_settled(resources):
        raise NativeWorkUncertainError(
            "native recovery continuation has unresolved owned resources"
        )


async def recover_native_cancel(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: _Graph,
    incarnation: RuntimeIncarnation,
    *,
    resources: HostedTurnResources | None,
) -> bool:
    """Before startup writes: ACK exact marker or certify only predecessor stop."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT native_work_id FROM agents_meta WHERE id=%s",
                (incarnation.agent_id,),
            )
        ).fetchone()
        command = await (
            await conn.execute(
                "SELECT acceptance FROM native_cancel_commands WHERE agent_id=%s "
                "AND outcome IN ('accepted','uncertain') ORDER BY accepted_at,id LIMIT 1",
                (incarnation.agent_id,),
            )
        ).fetchone()
    if command is None:
        return True
    accepted = NativeCancelAcceptance.model_validate(command[0])
    marker = NativeCancelMarker(command_id=accepted.command_id, target=accepted.target)
    if row is None or row[0] != marker.target.work_id:
        await _mark_uncertain(pool, marker)
        return False

    try:
        _require_settled_continuation(resources=resources)
        checkpoint_id = await cold_cancel_checkpoint(saver, marker)
        outcome = (
            NativeCancelOutcome.APPLIED
            if checkpoint_id is not None
            else NativeCancelOutcome.RECOVERED_STOPPED
        )
        recovery_id = (
            await _pause_certified_successor(pool, saver, graph, incarnation, marker)
            if checkpoint_id is None
            else None
        )
        _require_settled_continuation(resources=resources)
        async with async_write_transaction(pool) as conn:
            await finish_native_cancel(
                conn,
                incarnation,
                marker,
                outcome=outcome,
                checkpoint_id=checkpoint_id,
                recovery_checkpoint_id=recovery_id,
            )
    except (NativeWorkUncertainError, CheckpointReadError, ValidationError):
        await _mark_uncertain(pool, marker)
        logger.warning(
            "native cancel proof gap holds new work",
            event="native_cancel_uncertain",
            agent_id=incarnation.agent_id,
            work_id=str(marker.target.work_id),
        )
        return False
    return True


async def completed_native_cancel(
    pool: AsyncConnectionPool, target: NativeWorkTarget | None
) -> bool:
    if target is None:
        return False
    original = await command_for_work(pool, target)
    return original is not None and original[1] in (
        NativeCancelOutcome.APPLIED,
        NativeCancelOutcome.RECOVERED_STOPPED,
    )


async def halt_before_reinvoke(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: _Graph,
    incarnation: RuntimeIncarnation,
    target: NativeWorkTarget | None,
    config: RunnableConfig,
    *,
    resources: HostedTurnResources | None,
) -> dict[str, object] | None:
    """Recovery resumes the original accepted halt and its lifecycle flags."""
    if target is None or await command_for_work(pool, target) is None:
        return None
    await settle_native_invocation(
        pool, saver, graph, incarnation, target, config, resources=resources
    )
    reader = AsyncPostgresSaver(saver.conn, serde=saver.serde)
    tuple_ = await reader.aget_tuple({"configurable": {"thread_id": str(target.agent_id)}})
    if tuple_ is None:
        raise NativeWorkUncertainError("original native halt checkpoint is missing")
    values = tuple_.checkpoint["channel_values"]
    result: dict[str, object] = {"turn_idle": True}
    for field in ("exit_requested", "restart_requested"):
        value = values.get(field)
        if type(value) is not bool:
            raise NativeWorkUncertainError("original native lifecycle flags are missing")
        result[field] = value
    return result


async def hold_native_cancel(pool: AsyncConnectionPool, target: NativeWorkTarget | None) -> None:
    """Preserve accepted evidence when continuation settlement cannot be proved."""
    if target is not None:
        command = await command_for_work(pool, target)
        if command is not None:
            await _mark_uncertain(pool, command[0])


async def invoke_prepared_graph[T](
    control_pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    graph: _Graph,
    agent_id: int,
    ctx: AvaContext,
    config: RunnableConfig,
    work: NativeWorkContinuation,
    *,
    db: Database,
    bus: EventBus,
    relays: RelaySupervision,
    invoke: Callable[[AvaContext, dict[str, object]], Awaitable[T]],
    notes: HandoffNotes,
) -> dict[str, object] | T:
    """Execute the prepared original work within its retained resource scope."""
    incarnation = ctx.require_original_incarnation(agent_id)
    async with database_phase():
        halted = await halt_before_reinvoke(
            control_pool,
            checkpointer,
            graph,
            incarnation,
            work.target,
            config,
            resources=ctx.hosted_resources,
        )
        if halted is not None:
            return halted
        await settle_checkpoint(
            graph,
            db,
            bus,
            agent_id,
            relays,
            activate_accepted=False,
            incarnation=ctx.original_incarnation,
            resources=ctx.hosted_resources,
            notes=notes,
        )
        await prepare_native_invocation(control_pool, work, incarnation)
    return await invoke(
        replace(ctx, native_work=work.target),
        {
            "turn_active": False,
            "exit_requested": False,
            "turn_idle": False,
            "restart_requested": False,
            "native_work": work.target,
            "native_cancel": None,
        },
    )
