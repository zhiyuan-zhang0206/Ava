"""A durable original-result application permit; no saver or provider connection borrowing."""

from psycopg_pool import AsyncConnectionPool

from base.agents.compaction.execution import (
    CompactCommand,
    history_unchanged,
    require_receiver,
    settle,
)
from base.agents.compaction.models import CompactHeldError, CompactOutcome
from base.agents.history.checkpoint_cleanup import stamp_compact_checkpoint
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def authorize(
    pool: AsyncConnectionPool,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
) -> bool:
    """Freeze the application ordering before saver writes; later input stays pending."""
    if command.result is None or command.execution is None:
        raise CompactHeldError("compact application requires the durable original result")
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
        row = await (
            await conn.execute(
                "SELECT outcome,result FROM native_compact_commands WHERE id=%s "
                "AND attempt_id=%s AND released_at IS NULL FOR UPDATE",
                (command.acceptance.command_id, command.attempt_id),
            )
        ).fetchone()
        if row is None or row[1] != command.result.model_dump(mode="json"):
            raise CompactHeldError("compact application lost its immutable original result")
        outcome = CompactOutcome(row[0])
        if outcome is CompactOutcome.APPLYING:
            return True
        if outcome is not CompactOutcome.PREPARED:
            raise CompactHeldError("compact result is not eligible for application")
        cancel = await (
            await conn.execute(
                "SELECT 1 FROM native_cancel_commands WHERE work_id=%s "
                "AND outcome IN ('accepted','uncertain')",
                (command.execution.work_id,),
            )
        ).fetchone()
        if cancel is not None:
            await settle(
                conn,
                command,
                CompactOutcome.UNCERTAIN,
                reason="generation_cancelled",
                release=False,
            )
            return False
        if not await history_unchanged(conn, command):
            await settle(
                conn, command, CompactOutcome.REJECTED, reason="source_changed", release=False
            )
            return False
        if not await stamp_compact_checkpoint(
            conn,
            str(incarnation.agent_id),
            command.acceptance.target.checkpoint_id,
            closing=command.result.closing,
        ):
            raise CompactHeldError("compact original source anchor is absent or conflicting")
        await conn.execute(
            "UPDATE native_compact_commands SET outcome='applying' WHERE id=%s",
            (command.acceptance.command_id,),
        )
        return True
