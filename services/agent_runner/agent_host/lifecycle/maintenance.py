"""Final host continuation receipts; neither a graph node nor a pause hook."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.resource_admission import DRAINED_RESOURCES
from base.deploy.maintenance import admission
from base.log import logger
from services.agent_runner.agent_host.dispatcher import PendingInboundWake

FailureFences = dict[int, tuple[str | None, datetime | None]]


async def record_failure(agent_id: int, exc: BaseException, fences: FailureFences) -> None:
    # Fail closed before reading the journal: both read and write can fail.
    # The unknown generation remains a same-boot fence until explicit resume.
    fences[agent_id] = (None, None)
    current = admission.snapshot()
    if current is None:
        # A successful read proves this ordinary failure belongs to no hold.
        # An unreadable journal still leaves the unknown fence set above.
        fences.pop(agent_id, None)
        return
    category = type(exc).__name__
    hold = current.maintenance
    assert hold is not None  # noqa: S101 — snapshot() returns only holds
    if hold.outside_cohort(agent_id) or hold.settled_after_drain(agent_id):
        # Every runner sees every wake (the dispatcher's subscription is
        # cluster-wide), and a stop's shutdown cancels every task. A wake
        # outside the captured cohort (FC-10 F20: a stopping host latched
        # other machines' woken agents), or of a drained or parked member once
        # the drain is certified, runs no continuation: it is no receipt.
        # Both facts are read from the journal, without the database. An
        # uncaptured cohort and an uncertified drain still record.
        fences.pop(agent_id, None)
        logger.debug(
            "wake failure for agent {agent_id} not recorded: no continuation in "
            "this hold (phase {phase}, {category})",
            agent_id=agent_id,
            phase=hold.phase,
            category=category,
        )
        return
    fences[agent_id] = (current.holder, current.acquired_at)
    await asyncio.to_thread(admission.record_failure, agent_id, category)


async def record_drained(pool: AsyncConnectionPool, owner: UUID, agent_id: int) -> None:
    command_id = admission.pending_command(agent_id)
    if command_id is not None:
        # This is after the shielded graph continuation, resource closure
        # and final owner settlement; applied_at alone is not this proof.
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM agents_meta m JOIN inbound_messages i "  # noqa: S608 -- constant SQL fragment
                    "ON i.id=m.lifecycle_command_id AND i.agent_id=m.id "
                    "WHERE m.id=%s AND i.id=%s AND i.kind='restart' "
                    "AND i.status='claimed' AND i.applied_at IS NOT NULL "
                    "AND i.target_owner=%s "
                    "AND i.observed_at IS NULL AND m.runtime_owner IS NULL "
                    f"AND {DRAINED_RESOURCES}",
                    (agent_id, command_id, owner),
                )
            ).fetchone()
        if row is not None:
            await asyncio.to_thread(admission.record_drained, agent_id, command_id)


async def run_held(
    agent_id: int,
    status: str,
    fences: FailureFences,
    control: Callable[[int, str], Awaitable[None]],
) -> bool:
    current = admission.snapshot()
    if current is None or current.maintenance is None:
        fences.pop(agent_id, None)
        return False
    failed = agent_id in current.maintenance.failures or fences.get(agent_id) in (
        (None, None),
        (current.holder, current.acquired_at),
    )
    if not failed and admission.pending_command(agent_id) is not None:
        await control(agent_id, status)
    return True


def pending_wakes(fences: FailureFences) -> list[PendingInboundWake] | None:
    current = admission.snapshot()
    if current is None or current.maintenance is None:
        return None
    # A stale-turn cancel could interrupt the action maintenance is draining.
    # Persisted failures also fence fresh hosts until an explicit resume.
    return [
        PendingInboundWake(agent_id=agent, stale=False)
        for agent in current.maintenance.commands
        if agent not in current.maintenance.failures
        and admission.pending_command(agent) is not None
        and fences.get(agent) not in ((None, None), (current.holder, current.acquired_at))
    ]
