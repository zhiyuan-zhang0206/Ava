"""Reads of the understanding tree — the `understanding_nodes` table and the call record.

The write paths live with their writers: depth-1 nodes in `chunks.py` (`write_group_nodes`), the
levels above in `group_store.py` (`write_groups`). Both key a node by the deterministic span
identity `(agent_id, depth, span_start, span_end)`; boundaries are never trimmed (#1125), so the
stitched full history is append-only and message indices never shift.

Read paths:
- `load_nodes(agent_id)`, every node of the agent for the run-timeline serving merge (the window
  is applied there, on the read times);
- `load_call_records(agent_id)`, the cost-relevant slice of every understanding call
  (`understanding_chunk_calls`), for the per-node generation cost.

Nodes without timestamps are not served — they stay stored, just unservable until their time is
known.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection

from base.agents.history.hierarchy.usage import CallRecord
from base.db import Database

# The stored row shape's version; bump with a migration when columns change.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredNode:
    """One stored node row, in the shape the serving merge consumes."""

    id: int
    depth: int
    span_start: int
    span_end: int
    start_ts: datetime | None
    end_ts: datetime | None
    text: str
    parent_id: int | None
    engine_version: str
    prompt_version: str


@contextmanager
def _read_connection(db: Database) -> Generator[Connection[Any]]:
    """Borrow one short-lived autocommit read connection (checkpoint.py pattern)."""
    db_pool = db.pool(autocommit=True)
    try:
        with db_pool.connection() as conn:
            yield conn
    finally:
        db_pool.close()


def load_nodes(db: Database, agent_id: int) -> list[StoredNode]:
    """Every node of one agent, ordered by depth then message position."""
    with _read_connection(db) as conn:
        rows = conn.execute(
            """
            SELECT id, depth, span_start, span_end, start_ts, end_ts, text, parent_id,
                   engine_version, prompt_version
            FROM understanding_nodes
            WHERE agent_id = %s
            ORDER BY depth, span_start
            """,
            (agent_id,),
        ).fetchall()
    return [StoredNode(*row) for row in rows]


def load_call_records(db: Database, agent_id: int) -> list[CallRecord]:
    """Every understanding call of one agent, as the cost read needs it (no reply text)."""
    with _read_connection(db) as conn:
        rows = conn.execute(
            """
            SELECT j.compact_version, c.start_offset, c.prefix_len, c.usage_metadata, c.duration_ms
            FROM understanding_chunk_calls c
            JOIN understanding_chunk_jobs j ON j.id = c.job_id
            WHERE c.agent_id = %s
            """,
            (agent_id,),
        ).fetchall()
    return [CallRecord(int(v), int(so), int(pl), usage, float(ms)) for v, so, pl, usage, ms in rows]
