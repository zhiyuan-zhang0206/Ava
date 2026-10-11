"""Compaction boundary stamps on LangGraph checkpoints.

The graph runs on langgraph's default durability ("async" — the agent's
`graph.ainvoke` never passes `durability="exit"`; per-node checkpointing is a
deliberate design, see docs/decisions/agents/graph/2026-05-02-self-cycling-langgraph.md), so
the PostgresSaver writes one checkpoint row per super-step. Every checkpoint is
retained (never-delete ruling); a compaction instead stamps the newest
pre-compact checkpoint with `compact_boundary: true`, which anchors the
compaction segment for history reads and audits.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.history.closing_request import METADATA_KEY as CLOSING_METADATA_KEY
from base.agents.history.closing_request import ClosingRequest
from base.db.transaction import async_write_transaction

# Stamp the thread's newest checkpoint as a compaction boundary; the RETURNING
# names exactly the stamped boundary so the caller can enqueue the closing
# understanding chunk for it.
_MARK_BOUNDARY_SQL = (
    "UPDATE checkpoints SET metadata = metadata || %s::jsonb"
    " WHERE thread_id = %s AND checkpoint_ns = %s"
    "   AND checkpoint_id = ("
    "       SELECT checkpoint_id FROM checkpoints"
    "       WHERE thread_id = %s AND checkpoint_ns = %s"
    "       ORDER BY checkpoint_id DESC LIMIT 1)"
    " RETURNING checkpoint_id"
)


async def mark_compact_boundary(
    pool: AsyncConnectionPool,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
    closing: ClosingRequest | None = None,
) -> str | None:
    """Stamp the thread's newest checkpoint as a compaction boundary (idempotent).

    Returns the stamped checkpoint id (None when the thread has no checkpoint).
    `closing` (the compaction LLM call that read the segment whole) is written into the
    boundary's metadata so the history read can anchor the segment's tail.

    A compaction freezes the pre-compact history into a summary; the newest
    pre-compact checkpoint is the full-snapshot record of that segment, and the
    stamp anchors it for reads and audits. Callers are failure-tolerant: a
    missed stamp only loses segment traceability, never recoverability (the
    summary survives regardless).
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        stamp: dict[str, Any] = {"compact_boundary": True}
        if closing is not None:
            stamp[CLOSING_METADATA_KEY] = closing.to_metadata()
        await cur.execute(
            _MARK_BOUNDARY_SQL, (Jsonb(stamp), thread_id, checkpoint_ns, thread_id, checkpoint_ns)
        )
        row = await cur.fetchone()
    boundary = row[0] if row is not None else None
    return None if boundary is None else str(boundary)


async def stamp_compact_checkpoint(
    conn: psycopg.AsyncConnection,
    thread_id: str,
    checkpoint_id: str,
    *,
    closing: ClosingRequest | None = None,
) -> bool:
    """Stamp an exact retained source in the caller's application-permit transaction.

    Guarded replacement binds this historical checkpoint before graph writes;
    it must not stamp the subsequently materialized replacement as its source.
    Existing provider anchor evidence is immutable under a conflicting call.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("compact source stamping requires a caller-owned transaction")
    if not checkpoint_id:
        raise ValueError("compact source checkpoint ID must contain text")
    stamp: dict[str, Any] = {"compact_boundary": True}
    if closing is not None:
        stamp[CLOSING_METADATA_KEY] = closing.to_metadata()
    row = await (
        await conn.execute(
            "UPDATE checkpoints SET metadata=metadata || %s::jsonb "
            "WHERE thread_id=%s AND checkpoint_ns='' AND checkpoint_id=%s "
            "AND (%s::jsonb IS NULL OR NOT metadata ? %s OR metadata->%s=%s::jsonb) "
            "RETURNING checkpoint_id",
            (
                Jsonb(stamp),
                thread_id,
                checkpoint_id,
                None if closing is None else Jsonb(closing.to_metadata()),
                CLOSING_METADATA_KEY,
                CLOSING_METADATA_KEY,
                None if closing is None else Jsonb(closing.to_metadata()),
            ),
        )
    ).fetchone()
    return row is not None
