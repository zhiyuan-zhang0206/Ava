"""One durable lifecycle command pointer; no additional queue or polling loop.

Process and hosted dispatch share this acceptance boundary. All old consumers
must still be upgraded before activation: unconditional legacy claims are not
fenced by adding nullable columns. Acceptance never asserts that a process exited.

The pointer's whole life lives here: acceptance, superseded settlement, and the
admitted successor's restart observation, which clears it.
"""

from typing import Literal, TypedDict

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from agent.ownership.inbound import lock_inbound_owner
from base.agents.incarnation.lifecycle_acceptance import (
    LifecycleIntent,
    accept_lifecycle_command_async,
)
from base.native_process.runtime_incarnation import RuntimeIncarnation, current_incarnation

_OBSERVE_RESTART = (
    "UPDATE inbound_messages i SET observed_at=clock_timestamp(),status='done', "
    "payload=i.payload-'lifecycle_result' "
    "FROM agents_meta m WHERE m.id=%s AND m.runtime_generation=%s AND m.runtime_owner=%s "
    "AND m.lifecycle_command_id=i.id AND i.agent_id=m.id AND i.kind='restart' "
    "AND i.status='claimed' AND i.applied_at IS NOT NULL AND i.observed_at IS NULL "
    "AND (i.target_generation<>m.runtime_generation OR i.target_owner<>m.runtime_owner) "
    "RETURNING i.id"
)
_CLEAR_POINTER = (
    "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s AND lifecycle_command_id=%s"
)


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
    return await accept_lifecycle_command_async(conn, token)


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
    await conn.execute(_CLEAR_POINTER, (command.agent_id, command.id))
    return True


async def observe_hosted_admission(
    conn: psycopg.AsyncConnection, incarnation: RuntimeIncarnation
) -> None:
    """The admitted successor may observe restart before claiming new work.

    Successor admission acknowledges the restart in its own transaction.
    """
    cursor = await conn.execute(
        _OBSERVE_RESTART, (incarnation.agent_id, incarnation.generation, incarnation.owner)
    )
    observed = await cursor.fetchone()
    if observed is not None:
        await conn.execute(_CLEAR_POINTER, (incarnation.agent_id, observed[0]))
