"""Release the compact pointer only after original native execution and cancel closure."""

from psycopg_pool import AsyncConnectionPool

from base.agents.compaction.execution import CompactCommand, decode_command, require_receiver
from base.agents.compaction.models import CompactHeldError, CompactOutcome
from base.agents.incarnation.native_work_models import NativeCancelOutcome, NativeWorkPhase
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def release_completed(
    pool: AsyncConnectionPool,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
) -> bool:
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider "
                "FROM native_compact_commands WHERE id=%s AND released_at IS NULL FOR UPDATE",
                (command.acceptance.command_id,),
            )
        ).fetchone()
        if row is None:
            raise CompactHeldError("compact continuation release lost its original pointer")
        original = decode_command(row)
        if (
            original.acceptance != command.acceptance
            or original.execution != command.execution
            or original.attempt_id != command.attempt_id
            or original.result != command.result
        ):
            raise CompactHeldError("compact completion changed original identity or result")
        if original.outcome not in (CompactOutcome.APPLIED, CompactOutcome.REJECTED):
            return False
        if original.execution is None:
            raise CompactHeldError("compact completion lacks its fixed execution")
        work = await (
            await conn.execute(
                "SELECT phase,ended_at FROM native_graph_work WHERE id=%s FOR UPDATE",
                (original.execution.work_id,),
            )
        ).fetchone()
        if (
            work is None
            or NativeWorkPhase(work[0]) is not NativeWorkPhase.SETTLED
            or work[1] is None
        ):
            raise CompactHeldError("compact original execution has not actually closed")
        cancelled = await (
            await conn.execute(
                "SELECT outcome FROM native_cancel_commands WHERE work_id=%s",
                (original.execution.work_id,),
            )
        ).fetchone()
        if cancelled is not None and NativeCancelOutcome(cancelled[0]) not in (
            NativeCancelOutcome.APPLIED,
            NativeCancelOutcome.RECOVERED_STOPPED,
        ):
            raise CompactHeldError("compact original native cancellation remains unresolved")
        await conn.execute(
            "UPDATE native_compact_commands SET released_at=now() WHERE id=%s",
            (command.acceptance.command_id,),
        )
        return True
