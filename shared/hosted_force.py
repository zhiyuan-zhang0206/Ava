"""Hosted force acceptance and original-host quiescence on the existing command.

Only the live host's serialized turn pump may observe completion, after its real
continuation and owned exec cleanup return. Lease expiry, an empty cache, and
the host process dying are not proof that independent exec children ended.
"""

from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from shared.db_transaction import async_write_transaction


def install_hosted_force(conn: psycopg.Connection, agent_id: int, command_id: int) -> None:
    """Attach force to the current hosted incarnation under the caller's row lock."""
    row = conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s "
        "AND runtime_kind='hosted' AND runtime_generation IS NOT NULL "
        "AND runtime_owner IS NOT NULL FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None:
        return
    command = conn.execute(
        "UPDATE inbound_messages SET target_generation=%s,target_owner=%s,"
        "status='claimed',claimed_at=clock_timestamp(),applied_at=clock_timestamp() "
        "WHERE id=%s AND agent_id=%s AND kind='terminate' AND status='pending'",
        (row[0], row[1], command_id, agent_id),
    )
    if command.rowcount != 1:
        raise RuntimeError("hosted force lost its newly inserted command")
    conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s",
        (command_id, agent_id),
    )


async def original_host_force(
    pool: AsyncConnectionPool,
    agent_id: int,
    owner: UUID,
    machine: str,
    *,
    command_id: int | None = None,
    quiescent: bool = False,
) -> bool:
    """Validate the exact force, optionally settle in the original serialized pump.

    ``quiescent`` is an internal callsite contract, never accepted from HTTP or a
    payload: the host pump still excludes a replacement and its awaited work ended.
    The command's fixed target is checked against that host's actual boot owner.
    """
    async with async_write_transaction(pool) as conn:
        row = await (
            await conn.execute(
                "SELECT runtime_generation,lifecycle_command_id FROM agents_meta "
                "WHERE id=%s AND runtime_owner=%s AND machine=%s "
                "AND runtime_kind='hosted' AND status='terminated' FOR UPDATE",
                (agent_id, owner, machine),
            )
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return False
        if command_id is not None and row[1] != command_id:
            return False
        command = await (
            await conn.execute(
                "SELECT id FROM inbound_messages WHERE id=%s AND agent_id=%s "
                "AND kind='terminate' AND status='claimed' AND applied_at IS NOT NULL "
                "AND observed_at IS NULL AND target_generation=%s AND target_owner=%s "
                "FOR UPDATE",
                (row[1], agent_id, row[0], owner),
            )
        ).fetchone()
        if command is None:
            return False
        if quiescent:
            await conn.execute(
                "UPDATE inbound_messages SET observed_at=clock_timestamp(),status='done' "
                "WHERE id=%s",
                (row[1],),
            )
            await conn.execute(
                "UPDATE agents_meta SET lifecycle_command_id=NULL,lease_expires_at=NULL "
                "WHERE id=%s AND lifecycle_command_id=%s AND runtime_owner=%s "
                "AND runtime_generation=%s",
                (agent_id, row[1], owner, row[0]),
            )
        return True
