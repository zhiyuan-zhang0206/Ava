"""Final host continuation receipts; neither a graph node nor a pause hook."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from services.agent_host.dispatcher import PendingInboundWake
from shared import maintenance

FailureFences = dict[int, tuple[str | None, datetime | None]]


async def record_failure(agent_id: int, exc: BaseException, fences: FailureFences) -> None:
    # Fail closed before reading the journal: both read and write can fail.
    # The unknown generation remains a same-boot fence until explicit resume.
    fences[agent_id] = (None, None)
    current = maintenance.snapshot()
    if current is not None:
        fences[agent_id] = (current.holder, current.acquired_at)
        await asyncio.to_thread(maintenance.record_failure, agent_id, type(exc).__name__)


async def record_drained(pool: AsyncConnectionPool, owner: UUID, agent_id: int) -> None:
    command_id = maintenance.pending_command(agent_id)
    if command_id is not None:
        # This is after the shielded graph continuation, resource closure
        # and final owner settlement; applied_at alone is not this proof.
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM agents_meta m JOIN inbound_messages i "
                    "ON i.id=m.lifecycle_command_id AND i.agent_id=m.id "
                    "WHERE m.id=%s AND i.id=%s AND i.kind='restart' "
                    "AND i.status='claimed' AND i.applied_at IS NOT NULL "
                    "AND i.target_owner=%s "
                    "AND i.observed_at IS NULL AND m.runtime_owner IS NULL "
                    "AND m.incarnation_resources IS NULL",
                    (agent_id, command_id, owner),
                )
            ).fetchone()
        if row is not None:
            await asyncio.to_thread(maintenance.record_drained, agent_id, command_id)


async def run_held(
    agent_id: int,
    status: str,
    fences: FailureFences,
    control: Callable[[int, str], Awaitable[None]],
) -> bool:
    current = maintenance.snapshot()
    if current is None:
        fences.pop(agent_id, None)
        return False
    failed = fences.get(agent_id) in ((None, None), (current.holder, current.acquired_at))
    if not failed and maintenance.pending_command(agent_id) is not None:
        await control(agent_id, status)
    return True


def pending_wakes(fences: FailureFences) -> list[PendingInboundWake] | None:
    current = maintenance.snapshot()
    if current is None or current.maintenance is None:
        return None
    # A stale-turn cancel could interrupt the action maintenance is draining.
    return [
        PendingInboundWake(agent_id=agent, stale=False)
        for agent in current.maintenance.commands
        if maintenance.pending_command(agent) is not None
        and fences.get(agent) not in ((None, None), (current.holder, current.acquired_at))
    ]
