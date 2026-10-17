"""Final host continuation receipts; neither a graph node nor a pause hook."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.resource_admission import DRAINED_RESOURCES
from base.deploy.maintenance import admission
from base.log import logger

_END_UNAPPLIED_RESTART = (
    "UPDATE inbound_messages SET status='done', "
    "payload=COALESCE(payload,'{}'::jsonb) || %s "
    "WHERE id=%s AND agent_id=%s AND kind='restart' AND status IN ('pending','claimed') "
    "AND applied_at IS NULL RETURNING id"
)
_CLEAR_POINTER = (
    "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s AND lifecycle_command_id=%s"
)
_COMMAND_STATE = "SELECT status, applied_at IS NOT NULL FROM inbound_messages WHERE id=%s"


async def _end_unapplied_restart(pool: AsyncConnectionPool, agent_id: int, command_id: int) -> str:
    """End the owed restart command once, so nothing drives it again.

    Returns `ended` when this call settled it (the effect never started: it is
    marked done with a `failed` lifecycle result and the agent's pointer is
    cleared), `already-ended` when it was already terminal, and `applied` when
    the restart's effect had started: that command is left as it is, because an
    applied effect is neither undone nor declared unexecuted.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        result = {"lifecycle_result": {"outcome": "failed", "reason": "continuation_failed"}}
        cursor = await conn.execute(_END_UNAPPLIED_RESTART, (Jsonb(result), command_id, agent_id))
        if await cursor.fetchone() is not None:
            await conn.execute(_CLEAR_POINTER, (agent_id, command_id))
            return "ended"
        cursor = await conn.execute(_COMMAND_STATE, (command_id,))
        row = await cursor.fetchone()
    return "applied" if row is not None and row[1] and row[0] != "done" else "already-ended"


async def record_failure(pool: AsyncConnectionPool, agent_id: int, exc: BaseException) -> None:
    """End the restart command of a turn that failed while its continuation was owed.

    The command is settled `done` with a `failed` result (an existing terminal
    shape; nothing new is latched), ERROR is logged and `agent_continuation_lost`
    is raised once, and the pending-wake scan stops driving that agent. The agent
    stays in the hold: `meiva stop` ends at its drain deadline with the stall
    report, and a person takes it from there (`meiva start` releases the hold).
    A restart whose effect had already started is not rewritten; its failure is
    reported and the normal completion path stays.

    Only an agent with a pending restart command in a draining hold owes a
    continuation. Every runner sees every wake and a stop's shutdown cancels
    every task, so a failed wake of another machine's agent, of a drained or
    parked member, or outside any hold is no continuation failure and is not
    reported. Those facts come from the local journal, without the database.
    """
    command_id = admission.pending_command(agent_id)
    if command_id is None:
        return
    current = admission.snapshot()
    try:
        outcome = await _end_unapplied_restart(pool, agent_id, command_id)
    except Exception as ending_error:  # the failure may itself be the database outage
        outcome = f"not-ended ({type(ending_error).__name__})"
    if outcome == "already-ended":
        return
    logger.error(
        "agent {agent_id}: continuation failed ({failure_category}) at maintenance hold "
        "{holder}; restart command {command_id}: {outcome}",
        event="agent_continuation_lost",
        agent_id=agent_id,
        failure_category=type(exc).__name__,
        holder=current.holder if current is not None else None,
        command_id=command_id,
        outcome=outcome,
        reason=f"the turn raised during the drain; the restart command was {outcome}",
    )


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
    control: Callable[[int, str], Awaitable[None]],
) -> bool:
    current = admission.snapshot()
    if current is None or current.maintenance is None:
        return False
    if admission.pending_command(agent_id) is not None:
        await control(agent_id, status)
    return True
