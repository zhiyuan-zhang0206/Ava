"""Successor admission acknowledges restart observation in its own transaction."""

import psycopg

from shared.runtime_incarnation import RuntimeIncarnation

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


async def observe_hosted_admission(
    conn: psycopg.AsyncConnection, incarnation: RuntimeIncarnation
) -> None:
    """The admitted successor may observe restart before claiming new work."""
    cursor = await conn.execute(
        _OBSERVE_RESTART, (incarnation.agent_id, incarnation.generation, incarnation.owner)
    )
    observed = await cursor.fetchone()
    if observed is not None:
        await conn.execute(_CLEAR_POINTER, (incarnation.agent_id, observed[0]))
