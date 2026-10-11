"""Handoff an actually closed compact execution to its original guarded restart owner."""

from collections.abc import Callable
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import apply_hosted_lifecycle
from base.agents.compaction.closed import closed_original
from base.agents.compaction.execution import require_receiver
from base.agents.compaction.models import CompactHeldError
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
            raise CompactHeldError("interrupted compact restart requires inspection")
        await require_receiver(conn, command, incarnation)
    drop_agent(agent_id)
    return await _apply_original(pool, bus, original, command_id, resources=resources)


async def _apply_original(
    pool: AsyncConnectionPool,
    bus: EventBus,
    original: RuntimeIncarnation,
    command_id: int,
    *,
    resources: HostedTurnResources | None,
) -> bool:
    kind = await apply_hosted_lifecycle(
        pool, original, bus=bus, expected_command_id=command_id, resources=resources
    )
    if kind != "restart":
        raise CompactHeldError("original compact restart remains incomplete")
    return True
