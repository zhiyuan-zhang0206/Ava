"""One durable lifecycle command pointer; no additional queue or polling loop.

This acceptance protocol is not yet wired into dispatch. Existing consumers
must be upgraded before activation: old unconditional claims are not fenced
by adding nullable columns. Acceptance never asserts that a process exited.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from agent.inbound_ownership import lock_inbound_owner
from shared.runtime_incarnation import current_incarnation


@dataclass(frozen=True)
class LifecycleIntent:
    id: int
    agent_id: int
    kind: str
    generation: UUID
    owner: UUID
    accepted_at: datetime


class LifecycleNoopResult(TypedDict):
    outcome: Literal["superseded"]
    reason: Literal["target_replaced"]


async def accept_lifecycle_intent(
    conn: psycopg.AsyncConnection, agent_id: int
) -> LifecycleIntent | None:
    """Accept the oldest lifecycle request, or return the unfinished pointer.

    The caller owns the transaction. Lock ordering is agents_meta then inbound.
    Repeated calls preserve the first acceptance time and target; a second
    restart/terminate remains pending until explicit terminal settlement.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle acceptance requires an explicit transaction")
    await lock_inbound_owner(conn, agent_id)
    token = current_incarnation(agent_id)
    if token is None:
        raise RuntimeError("lifecycle acceptance requires an admitted runtime incarnation")
    cursor = await conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    )
    pointer = await cursor.fetchone()
    if pointer is None:
        raise RuntimeError("admitted runtime metadata disappeared during lifecycle acceptance")
    if pointer[0] is not None:
        cursor = await conn.execute(
            "SELECT id,agent_id,kind,target_generation,target_owner,claimed_at "
            "FROM inbound_messages WHERE id=%s AND agent_id=%s AND status='claimed' FOR UPDATE",
            (pointer[0], agent_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("lifecycle pointer does not reference an unfinished command")
        return LifecycleIntent(*row)
    cursor = await conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND status='pending' "
        "AND kind IN ('restart','terminate') ORDER BY id LIMIT 1 FOR UPDATE",
        (agent_id,),
    )
    pending = await cursor.fetchone()
    if pending is None:
        return None
    cursor = await conn.execute(
        "UPDATE inbound_messages SET status='claimed',claimed_at=clock_timestamp(), "
        "target_generation=%s,target_owner=%s WHERE id=%s AND status='pending' "
        "AND target_generation IS NULL AND target_owner IS NULL "
        "RETURNING id,agent_id,kind,target_generation,target_owner,claimed_at",
        (token.generation, token.owner, pending[0]),
    )
    accepted = await cursor.fetchone()
    if accepted is None:
        raise RuntimeError("pending lifecycle request already carries a target")
    await conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (pending[0], agent_id)
    )
    return LifecycleIntent(*accepted)


async def settle_superseded_intent(conn: psycopg.AsyncConnection, command: LifecycleIntent) -> bool:
    """Close only the recorded command after its exact target was replaced.

    A no-op is not an applied effect. This may be called by a replacement
    consumer/controller, but cannot relabel a still-current target as stale.
    The transaction caller must hold the controller's normal authority.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle settlement requires an explicit transaction")
    cursor = await conn.execute(
        "SELECT lifecycle_command_id,runtime_generation,runtime_owner "
        "FROM agents_meta WHERE id=%s FOR UPDATE",
        (command.agent_id,),
    )
    current = await cursor.fetchone()
    if current is None or current[0] != command.id:
        return False
    if current[1:] == (command.generation, command.owner):
        return False
    # Unknown ownership is not evidence of replacement.
    if current[1] is None or current[2] is None:
        return False
    result: LifecycleNoopResult = {"outcome": "superseded", "reason": "target_replaced"}
    cursor = await conn.execute(
        "UPDATE inbound_messages SET status='done',payload=COALESCE(payload,'{}'::jsonb) || %s "
        "WHERE id=%s AND agent_id=%s AND status='claimed' AND target_generation=%s "
        "AND target_owner=%s AND applied_at IS NULL RETURNING id",
        (
            Jsonb({"lifecycle_result": result}),
            command.id,
            command.agent_id,
            command.generation,
            command.owner,
        ),
    )
    if await cursor.fetchone() is None:
        return False
    await conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s AND lifecycle_command_id=%s",
        (command.agent_id, command.id),
    )
    return True
