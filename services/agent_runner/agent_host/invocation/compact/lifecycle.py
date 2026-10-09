"""Handoff an actually closed compact execution to its original guarded restart owner."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.ownership.hosted import apply_hosted_lifecycle
from agent.ownership.hosted_completion import completed_hosted_lifecycle_kind
from agent.ownership.lifecycle_intent import settle_superseded_intent
from base.agents.compaction.closed import closed_original
from base.agents.compaction.execution import require_receiver
from base.agents.compaction.models import CompactHeldError
from base.agents.incarnation.lifecycle_acceptance import accept_lifecycle_command_async
from base.agents.messages.native_restart import (
    completed_guarded_restart,
    original_guarded_restart_id,
    superseded_guarded_restart,
)
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources


async def settle_original_restart(
    pool: AsyncConnectionPool,
    bus: EventBus,
    agent_id: int,
    owner: UUID,
    drop_agent: Callable[[int], None],
    recover: Callable[[RuntimeIncarnation], Awaitable[None]],
    *,
    incarnation: RuntimeIncarnation | None = None,
    resources: HostedTurnResources | None,
) -> bool:
    """Return whether this exact original invocation ended through restart.

    A successor reads the original completed receipt before mutable receiver
    checks and does not restart or change an overlay again. Unknown/no-result
    compact uses this same seam only after its actual diagnostic closure.
    """
    command = await closed_original(pool, agent_id)
    if command is None or command.execution is None:
        return False
    original = RuntimeIncarnation(agent_id, command.execution.generation, command.execution.owner)
    async with pool.connection() as conn:
        command_id = await original_guarded_restart_id(conn, command.execution)
        if command_id is None:
            return False
        if await completed_guarded_restart(conn, original, command_id):
            return incarnation == original
        if await superseded_guarded_restart(conn, command.execution, command_id):
            return False
        if incarnation is None:
            row = await (
                await conn.execute(
                    "SELECT runtime_generation FROM agents_meta WHERE id=%s AND runtime_owner=%s "
                    "AND runtime_kind='hosted' AND lease_expires_at>clock_timestamp()",
                    (agent_id, owner),
                )
            ).fetchone()
            if row is None:
                raise CompactHeldError("original compact restart has no admitted receiver")
            incarnation = RuntimeIncarnation(agent_id, row[0], owner)
    async with async_write_transaction(pool) as conn:
        if incarnation != original:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM agents_meta WHERE id=%s AND runtime_generation=%s "
                    "AND runtime_owner=%s AND runtime_kind='hosted' AND lease_expires_at>clock_timestamp() "
                    "AND native_work_id=%s FOR UPDATE",
                    (
                        agent_id,
                        incarnation.generation,
                        incarnation.owner,
                        command.execution.work_id,
                    ),
                )
            ).fetchone()
            if row is None:
                raise CompactHeldError("compact restart no-effect lacks current admitted authority")
            intent = await accept_lifecycle_command_async(conn, incarnation)
            if intent is not None and intent.id == command_id:
                await settle_superseded_intent(conn, intent)
            if await superseded_guarded_restart(conn, command.execution, command_id):
                return False
            raise CompactHeldError("original compact restart lacks replaced-target proof")
        await require_receiver(conn, command, incarnation)
    drop_agent(agent_id)
    return await _apply_original(pool, bus, original, command_id, recover, resources=resources)


async def _apply_original(
    pool: AsyncConnectionPool,
    bus: EventBus,
    original: RuntimeIncarnation,
    command_id: int,
    recover: Callable[[RuntimeIncarnation], Awaitable[None]],
    *,
    resources: HostedTurnResources | None,
) -> bool:
    while True:
        try:
            kind = await apply_hosted_lifecycle(
                pool, original, bus=bus, expected_command_id=command_id, resources=resources
            )
            break
        except (psycopg.OperationalError, PoolTimeout):
            kind = await completed_hosted_lifecycle_kind(pool, original, command_id)
            if kind == "restart":
                break
            await recover(original)
    if kind is None:
        kind = await completed_hosted_lifecycle_kind(pool, original, command_id)
    if kind != "restart":
        raise CompactHeldError("original compact restart remains pending its canonical owner")
    return True
