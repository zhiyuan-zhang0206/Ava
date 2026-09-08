"""Final host continuation receipts; neither a graph node nor a pause hook."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from services.agent_host.dispatcher import PendingInboundWake
from shared import maintenance

FailureFences = dict[int, tuple[str | None, datetime | None]]

# The same database-outage family the turn loop and db_recovery already treat
# as a recoverable channel break (host.py `_invoke_until_done`, db_recovery).
# A turn raising one of these during a hold did not provably fail its
# continuation: the restart pointer and checkpoint survive exactly as after a
# host crash, and the crash-recovery path re-drives them. Such receipts are
# graded as crash-equivalent: recorded for audit, never latched as blocking.
CRASH_EQUIVALENT_FAILURES = (psycopg.OperationalError, PoolTimeout, TimeoutError)


async def record_failure(agent_id: int, exc: BaseException, fences: FailureFences) -> None:
    # Fail closed before reading the journal: both read and write can fail.
    # The unknown generation remains a same-boot fence until explicit resume.
    fences[agent_id] = (None, None)
    current = maintenance.snapshot()
    if current is None:
        # A successful read proves this ordinary failure belongs to no hold.
        # An unreadable journal still leaves the unknown fence set above.
        fences.pop(agent_id, None)
        return
    category = type(exc).__name__
    if isinstance(exc, CRASH_EQUIVALENT_FAILURES):
        # The receipt channel broke, not the continuation: the left-behind
        # state is crash-equivalent and durable. Record it for audit WITHOUT
        # latching a blocking failure or a fence — the next wake re-drives the
        # held-control path, whose explicit re-flush must succeed before the
        # restart is claimed and the drain can certify.
        fences.pop(agent_id, None)
        await asyncio.to_thread(maintenance.record_undelivered, agent_id, category)
        return
    fences[agent_id] = (current.holder, current.acquired_at)
    await asyncio.to_thread(maintenance.record_failure, agent_id, category)


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
    # Undelivered receipts carry no fence, so their agents keep waking: the
    # held-control re-drive is what completes a crash-equivalent receipt.
    return [
        PendingInboundWake(agent_id=agent, stale=False)
        for agent in current.maintenance.commands
        if maintenance.pending_command(agent) is not None
        and fences.get(agent) not in ((None, None), (current.holder, current.acquired_at))
    ]
