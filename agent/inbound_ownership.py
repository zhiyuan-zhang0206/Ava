"""Serialize queue mutation with the admitted consumer's runtime authority."""

import psycopg

from shared.runtime_incarnation import current_incarnation


class RuntimeOwnershipLostError(RuntimeError):
    """The caller must stop without changing a replacement runtime's state."""


async def lock_inbound_owner(conn: psycopg.AsyncConnection, agent_id: int) -> None:
    """Lock agents_meta before inbound rows in the caller's write transaction.

    Unknown legacy rows remain compatible, but an owned row never accepts a
    missing token. The lock serializes admission/replacement with claiming;
    clock_timestamp checks freshness after any lock wait, not at BEGIN time.
    This does not fence old binaries that still issue unconditional writes.
    """
    incarnation = current_incarnation(agent_id)
    cursor = await conn.execute(
        "SELECT runtime_generation, runtime_owner, runtime_kind, status, "
        "lease_expires_at FROM agents_meta WHERE id = %s FOR UPDATE",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if row is not None:
        generation, owner, kind, status, lease = row
        if incarnation is None:
            if generation is None and owner is None and kind is None:
                return
        elif (
            generation == incarnation.generation
            and owner == incarnation.owner
            and kind in ("process", "hosted")
            and status in ("running", "idling")
            and lease is not None
        ):
            clock = await conn.execute("SELECT %s > clock_timestamp()", (lease,))
            if await clock.fetchone() == (True,):
                return
    raise RuntimeOwnershipLostError(f"agent {agent_id} no longer owns a fresh runtime lease")
