"""One acceptance writer for live runtimes and verified cold controllers.

Callers establish their distinct authority before calling: a current owner with
a fresh lease, or a local controller that positively proved no admitted owner.
Both use this exact transaction and target fence. Chat never participates.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus

from shared.runtime_incarnation import RuntimeIncarnation

# Correlated against the unaliased agents_meta row. Selection is an optimization;
# the same predicate also fences the final automatic resurrection UPDATE.
FAILED_RESTART_FOR_CURRENT_TARGET: LiteralString = (
    "EXISTS(SELECT 1 FROM inbound_messages failed WHERE failed.agent_id=agents_meta.id "
    "AND failed.target_generation=agents_meta.runtime_generation "
    "AND failed.target_owner=agents_meta.runtime_owner AND failed.kind='restart' "
    "AND failed.status='done' AND failed.applied_at IS NOT NULL AND failed.observed_at IS NULL "
    "AND failed.payload->'lifecycle_result'->>'outcome'='failed' "
    "AND failed.payload->'lifecycle_result'->>'reason'='restart_deadline_expired')"
)


@dataclass(frozen=True)
class LifecycleIntent:
    id: int
    agent_id: int
    kind: str
    generation: UUID
    owner: UUID
    accepted_at: datetime


_ACCEPT = """
WITH target AS MATERIALIZED (
 SELECT id,lifecycle_command_id,runtime_generation,runtime_owner FROM agents_meta
 WHERE id=%s AND runtime_generation=%s AND runtime_owner=%s FOR UPDATE
), pending AS (
 SELECT i.id FROM inbound_messages i JOIN target t ON t.id=i.agent_id
 WHERE t.lifecycle_command_id IS NULL AND i.status='pending'
 AND i.kind IN ('restart','terminate') ORDER BY i.id LIMIT 1 FOR UPDATE OF i
), accepted AS (
 UPDATE inbound_messages i SET status='claimed',claimed_at=clock_timestamp(),
 target_generation=t.runtime_generation,target_owner=t.runtime_owner
 FROM pending p,target t WHERE i.id=p.id AND i.target_generation IS NULL AND i.target_owner IS NULL
 RETURNING i.id,i.agent_id,i.kind,i.target_generation,i.target_owner,i.claimed_at
), pointer AS (
 UPDATE agents_meta m SET lifecycle_command_id=a.id FROM accepted a
 WHERE m.id=a.agent_id AND m.lifecycle_command_id IS NULL RETURNING m.id
), chosen AS (
 SELECT i.id,i.agent_id,i.kind,i.target_generation,i.target_owner,i.claimed_at
 FROM target t JOIN inbound_messages i ON i.id=t.lifecycle_command_id AND i.agent_id=t.id
 WHERE i.status='claimed'
 UNION ALL SELECT * FROM accepted
)
SELECT t.lifecycle_command_id,c.*,(SELECT count(*) FROM pending)
FROM target t LEFT JOIN chosen c ON c.agent_id=t.id
CROSS JOIN (SELECT count(*) FROM pointer) written
"""


def _decode(row: tuple[Any, ...] | None) -> LifecycleIntent | None:
    if row is None:
        raise RuntimeError("lifecycle acceptance target incarnation changed")
    if row[1] is None:
        if row[0] is not None:
            raise RuntimeError("lifecycle pointer does not reference an unfinished command")
        if row[7]:
            raise RuntimeError("pending lifecycle request already carries a target")
        return None
    return LifecycleIntent(*row[1:7])


def accept_lifecycle_command(
    conn: psycopg.Connection, target: RuntimeIncarnation
) -> LifecycleIntent | None:
    """Caller retains its ownership/absence proof lock through this write."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle acceptance requires an explicit transaction")
    return _decode(
        conn.execute(_ACCEPT, (target.agent_id, target.generation, target.owner)).fetchone()
    )


async def accept_lifecycle_command_async(
    conn: psycopg.AsyncConnection, target: RuntimeIncarnation
) -> LifecycleIntent | None:
    """Async transport for the same SQL writer; no alternate admission rules."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle acceptance requires an explicit transaction")
    cursor = await conn.execute(_ACCEPT, (target.agent_id, target.generation, target.owner))
    return _decode(await cursor.fetchone())
