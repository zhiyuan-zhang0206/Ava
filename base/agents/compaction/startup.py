"""A typed retained compact continuation precedes native cancellation recovery."""

from psycopg_pool import AsyncConnectionPool

from base.agents.compaction.execution import CompactCommand, decode_command, require_receiver
from base.agents.compaction.models import CompactOutcome
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def resumable_compact(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
) -> CompactCommand | None:
    """Never authorize provider work or ordinary claim from a status/lease shortcut.

    PREPARED still requires the original application authorization (including
    cancellation ordering). APPLYING resumes only its durable original result.
    """
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (incarnation.agent_id,)
        )
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider "
                "FROM native_compact_commands WHERE agent_id=%s AND released_at IS NULL FOR UPDATE",
                (incarnation.agent_id,),
            )
        ).fetchone()
        if row is None:
            return None
        command = decode_command(row)
        if command.result is None or command.outcome not in (
            CompactOutcome.PREPARED,
            CompactOutcome.APPLYING,
            CompactOutcome.APPLIED,
            CompactOutcome.REJECTED,
        ):
            return None
        await require_receiver(conn, command, incarnation)
        return command
