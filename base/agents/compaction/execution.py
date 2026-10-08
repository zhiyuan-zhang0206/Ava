"""Original compact attempt/result CAS under the native work and receiver owners."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.compaction.history import ADMISSION_SQL, PENDING_HISTORY_SQL
from base.agents.compaction.models import (
    CompactAcceptance,
    CompactHeldError,
    CompactOutcome,
    PreparedSummary,
)
from base.agents.compaction.source_owner import require_source_receiver
from base.agents.incarnation.native_work import load_work, prepare_work, receiver
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


@dataclass(frozen=True)
class CompactCommand:
    acceptance: CompactAcceptance
    outcome: CompactOutcome
    attempt_id: UUID | None
    execution: NativeWorkTarget | None
    result: PreparedSummary | None
    attempt_provider: str | None = None


def decode_command(row: tuple[Any, ...]) -> CompactCommand:
    command = CompactCommand(
        CompactAcceptance.model_validate(row[0]),
        CompactOutcome(row[1]),
        row[2],
        None if row[3] is None else NativeWorkTarget.model_validate(row[3]),
        None if row[4] is None else PreparedSummary.model_validate(row[4]),
        row[5],
    )
    if command.result is not None and command.result.attempt_id != command.attempt_id:
        raise CompactHeldError("prepared compact result belongs to another attempt")
    return command


async def pending(pool: AsyncConnectionPool, agent_id: int) -> CompactCommand | None:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider FROM native_compact_commands "
                "WHERE agent_id=%s AND released_at IS NULL",
                (agent_id,),
            )
        ).fetchone()
    return None if row is None else decode_command(row)


async def completed_original(pool: AsyncConnectionPool, command: CompactCommand) -> bool:
    """Reconcile a lost terminal commit response using only immutable original identity."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider,released_at "
                "FROM native_compact_commands WHERE id=%s",
                (command.acceptance.command_id,),
            )
        ).fetchone()
    if row is None:
        raise CompactHeldError("original compact receipt disappeared")
    stored = decode_command(row[:6])
    if stored.acceptance != command.acceptance:
        raise CompactHeldError("original compact acceptance identity changed")
    if row[6] is None:
        return False
    if (
        stored.acceptance != command.acceptance
        or stored.attempt_id != command.attempt_id
        or stored.execution != command.execution
    ):
        raise CompactHeldError("original compact completion identity changed")
    if command.result is not None and stored.result != command.result:
        raise CompactHeldError("original compact completion result changed")
    return True


async def history_unchanged(conn: psycopg.AsyncConnection, command: CompactCommand) -> bool:
    target = command.acceptance.target
    row = await (
        await conn.execute(
            "SELECT checkpoint->'channel_versions'->>'messages',checkpoint->'channel_versions'->>'compact' "
            "FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
            (str(target.source.agent_id),),
        )
    ).fetchone()
    anchor = await (
        await conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' AND checkpoint_id=%s",
            (str(target.source.agent_id), target.checkpoint_id),
        )
    ).fetchone()
    admission = await (await conn.execute(ADMISSION_SQL, (target.source.agent_id,))).fetchone()
    pending_write = await (
        await conn.execute(PENDING_HISTORY_SQL, (str(target.source.agent_id),) * 2)
    ).fetchone()
    return (
        pending_write is None
        and admission is None
        and anchor is not None
        and row == (target.messages_version, target.compact_channel_version)
    )


async def require_receiver(
    conn: psycopg.AsyncConnection,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
) -> None:
    await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (incarnation.agent_id,))
    row = await (
        await conn.execute(
            "SELECT machine,runtime_generation,runtime_owner,incarnation_resources,native_work_id "
            "FROM agents_meta WHERE id=%s AND runtime_kind='hosted' "
            "AND lease_expires_at>clock_timestamp() AND NOT EXISTS ("
            "SELECT 1 FROM agent_impersonations p WHERE p.agent_id=agents_meta.id AND p.status='active')",
            (incarnation.agent_id,),
        )
    ).fetchone()
    if row is None or command.execution is None:
        raise CompactHeldError("compact execution has no current managed receiver")
    work = await load_work(conn, command.execution.work_id, lock=True)
    evidence = decode_resources(row[3])
    if (
        work is None
        or work.target != command.execution
        or receiver(work) != (row[0], incarnation.generation, incarnation.owner)
        or (row[1], row[2]) != (incarnation.generation, incarnation.owner)
        or row[4] != command.execution.work_id
        or not isinstance(evidence, IncarnationResources)
        or (evidence.generation, evidence.owner) != (incarnation.generation, incarnation.owner)
        or evidence.host_process is None
        or evidence.frozen_by is not None
        or evidence.requests
    ):
        raise CompactHeldError("compact executor lacks exact transfer and closed-resource evidence")


async def claim_attempt(
    pool: AsyncConnectionPool,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
    *,
    provider_key: str,
) -> tuple[CompactCommand, bool]:
    if not isinstance(provider_key, str) or not provider_key:
        raise ValueError("compact attempt requires its actual registered provider key")
    target = command.acceptance.target
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (incarnation.agent_id,)
        )
        row = await (
            await conn.execute(
                "SELECT machine,native_work_id,runtime_generation,runtime_owner FROM agents_meta "
                "WHERE id=%s AND runtime_kind='hosted' AND lease_expires_at>clock_timestamp() "
                "AND NOT EXISTS (SELECT 1 FROM agent_impersonations p WHERE p.agent_id=agents_meta.id AND p.status='active')",
                (incarnation.agent_id,),
            )
        ).fetchone()
        stored = await (
            await conn.execute(
                "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider FROM native_compact_commands "
                "WHERE id=%s AND released_at IS NULL FOR UPDATE",
                (command.acceptance.command_id,),
            )
        ).fetchone()
        if stored is None:
            raise CompactHeldError("compact command was already released")
        previous = decode_command(stored)
        if previous.attempt_id is not None:
            return previous, False
        await require_source_receiver(conn, previous.acceptance, incarnation)
        if row is None or (row[2], row[3]) != (incarnation.generation, incarnation.owner):
            raise CompactHeldError("compact source claim lost its actual admitted owner")
        if row[1] != target.source.work_id:
            raise CompactHeldError("compact source work pointer changed before generation claim")
        if not await history_unchanged(conn, previous):
            await settle(conn, previous, CompactOutcome.REJECTED, reason="source_changed")
            return CompactCommand(
                previous.acceptance, CompactOutcome.REJECTED, None, None, None
            ), False
        source = await load_work(conn, target.source.work_id, lock=True)
        if source is None or source.target != target.source or source.phase.value != "settled":
            raise CompactHeldError("compact source is not original ended work")
        execution = NativeWorkTarget(
            protocol=1,
            work_id=uuid4(),
            agent_id=incarnation.agent_id,
            machine=row[0],
            generation=incarnation.generation,
            owner=incarnation.owner,
        )
        if (
            await prepare_work(conn, execution, compact_command_id=command.acceptance.command_id)
            is None
        ):
            raise CompactHeldError("compact execution lacks admitted native resources")
        attempt = uuid4()
        await conn.execute(
            "UPDATE native_compact_commands SET attempt_id=%s,execution=%s,attempt_provider=%s WHERE id=%s",
            (
                attempt,
                Jsonb(execution.model_dump(mode="json")),
                provider_key,
                command.acceptance.command_id,
            ),
        )
        return CompactCommand(
            previous.acceptance, previous.outcome, attempt, execution, None, provider_key
        ), True


async def save_result(
    pool: AsyncConnectionPool,
    command: CompactCommand,
    incarnation: RuntimeIncarnation,
    result: PreparedSummary,
) -> CompactCommand:
    if result.attempt_id != command.attempt_id:
        raise CompactHeldError("compact result has another original attempt")
    async with async_write_transaction(pool) as conn:
        await require_receiver(conn, command, incarnation)
        row = await (
            await conn.execute(
                "SELECT acceptance,outcome,attempt_id,execution,result,attempt_provider FROM native_compact_commands "
                "WHERE id=%s AND attempt_id=%s AND released_at IS NULL FOR UPDATE",
                (command.acceptance.command_id, command.attempt_id),
            )
        ).fetchone()
        if row is None:
            raise CompactHeldError("original compact attempt was released")
        previous = decode_command(row)
        if previous.result is not None:
            if previous.result != result:
                raise CompactHeldError("original compact result cannot be replaced")
            return previous
        if previous.outcome != CompactOutcome.ACCEPTED:
            raise CompactHeldError(
                "unknown compact attempt cannot automatically apply a late result"
            )
        await conn.execute(
            "UPDATE native_compact_commands SET result=%s,outcome='prepared' WHERE id=%s",
            (Jsonb(result.model_dump(mode="json")), command.acceptance.command_id),
        )
        return CompactCommand(
            previous.acceptance,
            CompactOutcome.PREPARED,
            previous.attempt_id,
            previous.execution,
            result,
            previous.attempt_provider,
        )


async def settle(
    conn: psycopg.AsyncConnection,
    command: CompactCommand,
    outcome: CompactOutcome,
    *,
    reason: str | None = None,
    checkpoint_id: str | None = None,
    recovery_checkpoint_id: str | None = None,
    release: bool = True,
) -> None:
    cursor = await conn.execute(
        "UPDATE native_compact_commands SET outcome=%s,reason=%s,checkpoint_id=%s,recovery_checkpoint_id=%s, "
        "released_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s AND released_at IS NULL "
        "AND attempt_id IS NOT DISTINCT FROM %s RETURNING id",
        (
            outcome.value,
            reason,
            checkpoint_id,
            recovery_checkpoint_id,
            release,
            command.acceptance.command_id,
            command.attempt_id,
        ),
    )
    if await cursor.fetchone() is None:
        row = await (
            await conn.execute(
                "SELECT outcome,reason,checkpoint_id,recovery_checkpoint_id,attempt_id,released_at IS NOT NULL "
                "FROM native_compact_commands WHERE id=%s",
                (command.acceptance.command_id,),
            )
        ).fetchone()
        if row != (
            outcome.value,
            reason,
            checkpoint_id,
            recovery_checkpoint_id,
            command.attempt_id,
            release,
        ):
            raise CompactHeldError("compact settlement lost its original pending attempt")
