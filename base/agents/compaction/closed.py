"""Locate retained original execution closure; the pointer is never a substitute target."""

from psycopg_pool import AsyncConnectionPool

from base.agents.compaction.execution import CompactCommand, decode_command
from base.agents.compaction.models import CompactHeldError


async def closed_original(pool: AsyncConnectionPool, agent_id: int) -> CompactCommand | None:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT c.acceptance,c.outcome,c.attempt_id,c.execution,c.result,c.attempt_provider "
                "FROM native_compact_commands c JOIN agents_meta m ON m.id=c.agent_id "
                "JOIN native_graph_work w ON w.id=m.native_work_id "
                "WHERE m.id=%s AND c.execution->>'work_id'=w.id::text "
                "AND c.released_at IS NOT NULL AND w.phase='settled' AND w.ended_at IS NOT NULL",
                (agent_id,),
            )
        ).fetchone()
    if row is None:
        return None
    command = decode_command(row)
    if command.execution is None or command.execution.agent_id != agent_id:
        raise CompactHeldError("released compact original execution identity differs")
    return command
