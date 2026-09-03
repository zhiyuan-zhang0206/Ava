"""Install a process lifecycle decision, not a claim of process disappearance."""

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from shared.lifecycle_process_identity import capture_process_identity
from shared.runtime_incarnation import current_incarnation


async def apply_process_lifecycle(
    conn: psycopg.AsyncConnection, agent_id: int, command_id: int
) -> bool:
    """Atomically install the current target's decision and applied timestamp.

    Returning True means the durable decision is installed. It does not mean
    SIGTERM succeeded or a replacement has started. Applied replay cannot
    target a successor; the original executor must end instead of claiming
    another command. Hosted application belongs to its single-flight boundary.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle apply requires an explicit transaction")
    token = current_incarnation(agent_id)
    if token is None:
        raise RuntimeError("lifecycle apply requires an admitted incarnation")
    cursor = await conn.execute(
        "SELECT runtime_generation,runtime_owner,runtime_kind,lifecycle_command_id,"
        "status,lease_expires_at,pid,machine FROM agents_meta WHERE id=%s FOR UPDATE",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if row is None or row[:4] != (token.generation, token.owner, "process", command_id):
        return False
    cursor = await conn.execute(
        "SELECT kind,applied_at FROM inbound_messages WHERE id=%s AND agent_id=%s "
        "AND target_generation=%s AND target_owner=%s AND status='claimed' FOR UPDATE",
        (command_id, agent_id, token.generation, token.owner),
    )
    command = await cursor.fetchone()
    if command is None:
        return False
    if command[1] is not None:
        return True
    from shared.resource_admission import require_resources_closed_async

    await require_resources_closed_async(conn, agent_id)
    if row[4] not in ("running", "idling") or row[5] is None:
        return False
    cursor = await conn.execute("SELECT %s > clock_timestamp()", (row[5],))
    if await cursor.fetchone() != (True,):
        return False
    identity = capture_process_identity(row[6], row[7])
    if command[0] == "restart":
        await conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (agent_id,))
    elif command[0] == "terminate":
        await conn.execute(
            "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
            (agent_id,),
        )
    else:
        raise ValueError(f"not an executable lifecycle command: {command[0]}")
    await conn.execute(
        "UPDATE inbound_messages SET applied_at=clock_timestamp(),"
        "payload=COALESCE(payload,'{}'::jsonb)||%s WHERE id=%s",
        (Jsonb({"target_process_identity": identity}), command_id),
    )
    return True
