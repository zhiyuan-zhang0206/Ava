"""Durable native invocation facts under the existing metadata owner lock."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.sql import SQL
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, TypeAdapter

from base.agents.incarnation.native_work_models import (
    NativeWorkPhase,
    NativeWorkTarget,
    NativeWorkUncertainError,
)
from base.agents.incarnation.resource_transfer import ResourceTransferProof
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.native_process.runtime_incarnation import RuntimeIncarnation


class NativeWorkTransfer(BaseModel):
    """An actual admission proof appended in the same successful transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_machine: str
    target_machine: str
    proof: ResourceTransferProof


@dataclass(frozen=True)
class NativeWorkRecord:
    target: NativeWorkTarget
    phase: NativeWorkPhase
    transfers: list[NativeWorkTransfer]


_TRANSFERS = TypeAdapter(list[NativeWorkTransfer])


def decode_work(row: tuple[Any, ...]) -> NativeWorkRecord:
    return NativeWorkRecord(
        NativeWorkTarget(
            work_id=row[0],
            agent_id=row[1],
            machine=row[2],
            generation=row[3],
            owner=row[4],
            protocol=row[5],
        ),
        NativeWorkPhase(row[6]),
        _TRANSFERS.validate_python(row[7]),
    )


async def load_work(
    conn: psycopg.AsyncConnection, work_id: UUID, *, lock: bool = False
) -> NativeWorkRecord | None:
    row = await (
        await conn.execute(
            SQL(
                "SELECT id,agent_id,machine,generation,owner,protocol,phase,transfer_chain "
                "FROM native_graph_work WHERE id=%s"
            )
            + SQL(" FOR UPDATE" if lock else ""),
            (work_id,),
        )
    ).fetchone()
    return None if row is None else decode_work(row)


def managed_resources(value: object, target: NativeWorkTarget) -> bool:
    """A real admitted managed set, never NULL or a version-column inference."""
    if value is None:
        return False
    resources = decode_resources(value)
    return (
        isinstance(resources, IncarnationResources)
        and (resources.generation, resources.owner) == (target.generation, target.owner)
        and resources.host_process is not None
        and resources.frozen_by is None
    )


async def pending_command(conn: psycopg.AsyncConnection, work_id: UUID) -> UUID | None:
    row = await (
        await conn.execute(
            "SELECT id FROM native_cancel_commands WHERE work_id=%s "
            "AND outcome IN ('accepted','uncertain')",
            (work_id,),
        )
    ).fetchone()
    return None if row is None else row[0]


async def prepare_work(
    conn: psycopg.AsyncConnection,
    target: NativeWorkTarget,
    *,
    compact_command_id: UUID | None = None,
) -> NativeWorkTarget | None:
    """Retain the metadata lock and caller transaction through pointer admission."""
    row = await (
        await conn.execute(
            "SELECT incarnation_resources,native_work_id FROM agents_meta WHERE id=%s "
            "AND machine=%s AND runtime_generation=%s AND runtime_owner=%s "
            "AND runtime_kind='hosted' AND lease_expires_at>clock_timestamp() FOR UPDATE",
            (target.agent_id, target.machine, target.generation, target.owner),
        )
    ).fetchone()
    if row is None:
        raise NativeWorkUncertainError("native work has no admitted owner")
    compact = await (
        await conn.execute(
            "SELECT id,execution->>'work_id' FROM native_compact_commands "
            "WHERE agent_id=%s AND released_at IS NULL",
            (target.agent_id,),
        )
    ).fetchone()
    if compact is not None and (
        compact[0] != compact_command_id
        or (compact[1] is not None and compact[1] != str(target.work_id))
    ):
        raise NativeWorkUncertainError("original guarded compact must settle before new work")
    protected = await (
        await conn.execute(
            "SELECT work_id FROM native_cancel_commands WHERE agent_id=%s "
            "AND outcome IN ('accepted','uncertain') AND work_id<>%s LIMIT 1",
            (target.agent_id, target.work_id),
        )
    ).fetchone()
    if protected is not None:
        raise NativeWorkUncertainError("original native cancel must settle before new work")
    old_id = row[1]
    if old_id is not None and old_id != target.work_id:
        if await pending_command(conn, old_id) is not None:
            raise NativeWorkUncertainError("original native cancel must settle before new work")
        await conn.execute(
            "UPDATE native_graph_work SET phase='abandoned',ended_at=now() "
            "WHERE id=%s AND phase IN ('preparing','active','uncertain')",
            (old_id,),
        )
    if not managed_resources(row[0], target):
        await conn.execute(
            "UPDATE agents_meta SET native_work_id=NULL WHERE id=%s", (target.agent_id,)
        )
        return None
    await conn.execute(
        "INSERT INTO native_graph_work(id,agent_id,machine,generation,owner,protocol,phase) "
        "VALUES (%s,%s,%s,%s,%s,1,'preparing') ON CONFLICT(id) DO NOTHING",
        (target.work_id, target.agent_id, target.machine, target.generation, target.owner),
    )
    stored = await load_work(conn, target.work_id, lock=True)
    if stored is None or stored.target != target:
        raise NativeWorkUncertainError("native work identity conflict")
    await conn.execute(
        "UPDATE agents_meta SET native_work_id=%s WHERE id=%s", (target.work_id, target.agent_id)
    )
    return target


async def activate_work(conn: psycopg.AsyncConnection, target: NativeWorkTarget) -> None:
    row = await (
        await conn.execute(
            "SELECT native_work_id FROM agents_meta WHERE id=%s AND machine=%s "
            "AND runtime_generation=%s AND runtime_owner=%s AND runtime_kind='hosted' "
            "AND lease_expires_at>clock_timestamp() FOR UPDATE",
            (target.agent_id, target.machine, target.generation, target.owner),
        )
    ).fetchone()
    if row != (target.work_id,):
        raise NativeWorkUncertainError("native work lost its original owner")
    await conn.execute(
        "UPDATE native_graph_work SET phase='active' WHERE id=%s AND phase='preparing'",
        (target.work_id,),
    )


def receiver(work: NativeWorkRecord) -> tuple[str, UUID, UUID]:
    """Validate every retained hop from the immutable original tuple."""
    end = (work.target.machine, work.target.generation, work.target.owner)
    for transfer in work.transfers:
        proof = transfer.proof
        if (
            proof.agent_id != work.target.agent_id
            or (transfer.source_machine, proof.source_generation, proof.source_owner) != end
        ):
            raise NativeWorkUncertainError("native work transfer chain is incomplete")
        end = (transfer.target_machine, proof.target_generation, proof.target_owner)
    return end


async def certify_transfer(
    conn: psycopg.AsyncConnection,
    proof: ResourceTransferProof | None,
    *,
    work_id: UUID | None,
    source_machine: str,
    target_machine: str,
    incarnation: RuntimeIncarnation,
) -> None:
    """Append only actual resource proof after successful same-TX admission."""
    if work_id is None or proof is None:
        return
    work = await load_work(conn, work_id, lock=True)
    if work is None or work.phase in (NativeWorkPhase.SETTLED, NativeWorkPhase.ABANDONED):
        return
    if (proof.agent_id, proof.target_generation, proof.target_owner) != (
        incarnation.agent_id,
        incarnation.generation,
        incarnation.owner,
    ) or receiver(work) != (source_machine, proof.source_generation, proof.source_owner):
        return
    chain = [item.model_dump(mode="json") for item in work.transfers]
    chain.append(
        NativeWorkTransfer(
            source_machine=source_machine, target_machine=target_machine, proof=proof
        ).model_dump(mode="json")
    )
    await conn.execute(
        "UPDATE native_graph_work SET transfer_chain=%s WHERE id=%s", (Jsonb(chain), work_id)
    )
