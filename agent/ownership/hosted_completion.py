"""Original hosted lifecycle identity and its durable completion evidence."""

from typing import Literal, cast

from psycopg_pool import AsyncConnectionPool

from base.agents.messages.native_restart import completed_guarded_restart
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def pending_hosted_lifecycle_id(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> int | None:
    """Freeze only this incarnation's claimed, unapplied lifecycle pointer."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT i.id FROM agents_meta m JOIN inbound_messages i "
                "ON i.id=m.lifecycle_command_id WHERE m.id=%s "
                "AND m.runtime_generation=%s AND m.runtime_owner=%s "
                "AND i.agent_id=m.id AND i.target_generation=%s AND i.target_owner=%s "
                "AND i.kind IN ('restart','terminate') AND i.status='claimed' "
                "AND i.applied_at IS NULL",
                (
                    incarnation.agent_id,
                    incarnation.generation,
                    incarnation.owner,
                    incarnation.generation,
                    incarnation.owner,
                ),
            )
        ).fetchone()
    return None if row is None else row[0]


async def completed_hosted_lifecycle_kind(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation, command_id: int
) -> Literal["restart", "terminate"] | None:
    """Read the original receipt; current status or replacement owner is no proof."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT kind FROM inbound_messages WHERE id=%s AND agent_id=%s "
                "AND target_generation=%s AND target_owner=%s AND applied_at IS NOT NULL "
                "AND ((kind='restart' AND (status='claimed' OR (status='done' AND observed_at IS NOT NULL))) "
                "OR (kind='terminate' AND status='done' AND observed_at IS NOT NULL))",
                (command_id, incarnation.agent_id, incarnation.generation, incarnation.owner),
            )
        ).fetchone()
        if row is None and await completed_guarded_restart(conn, incarnation, command_id):
            return "restart"
    return None if row is None else cast(Literal["restart", "terminate"], row[0])
