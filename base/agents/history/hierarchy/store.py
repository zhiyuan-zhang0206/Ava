"""Reads of the understanding tree — the `understanding_nodes` table and the call record.

The write paths live with their writers: depth-1 nodes in `chunks.py` (`write_group_nodes`), the
levels above in `group_store.py` (`write_groups`). Both key a node by the deterministic span
identity `(agent_id, depth, span_start, span_end)`; boundaries are never trimmed (#1125), so the
stitched full history is append-only and message indices never shift.

Read paths:
- `load_nodes(agent_id)`, every node of the agent for the run-timeline serving merge (the window
  is applied there, on the read times);
- `load_generation_costs(agent_id)`, what each chunk job's and each grouping check's calls cost
  (from `understanding_chunk_calls` / `understanding_group_calls`), joined to nodes by `job_id` /
  `check_key`.

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

from base.agents.history.hierarchy.usage import GenerationUsage
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
    job_id: int | None = None
    check_key: str | None = None


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
    """Every node of this pipeline of one agent (`chunk-*` / `group-*` engine versions: a node an
    older release's worker wrote in a mixed-version window is not part of this tree), ordered by
    depth then message position."""
    with _read_connection(db) as conn:
        rows = conn.execute(
            """
            SELECT id, depth, span_start, span_end, start_ts, end_ts, text, parent_id,
                   engine_version, prompt_version, job_id, check_key
            FROM understanding_nodes
            WHERE agent_id = %s
              AND (engine_version LIKE 'chunk-%%' OR engine_version LIKE 'group-%%')
            ORDER BY depth, span_start
            """,
            (agent_id,),
        ).fetchall()
    return [StoredNode(*row) for row in rows]


def _costs(rows: list[Any]) -> dict[Any, GenerationUsage]:
    return {
        key: GenerationUsage(int(calls), int(inp), int(cache), int(out), float(ms) / 1000)
        for key, calls, inp, cache, out, ms in rows
    }


def load_generation_costs(
    db: Database, agent_id: int
) -> tuple[dict[int, GenerationUsage], dict[str, GenerationUsage]]:
    """What generating the agent's nodes cost, from the raw record of every understanding call:
    by chunk job id (the level-1 nodes of that job share it) and by grouping check key (the
    nodes that check wrote share it)."""
    with _read_connection(db) as conn:
        jobs = conn.execute(
            """
            SELECT job_id, count(*), coalesce(sum((usage_metadata->>'input_tokens')::bigint), 0),
                   coalesce(sum((usage_metadata->'input_token_details'->>'cache_read')::bigint), 0),
                   coalesce(sum((usage_metadata->>'output_tokens')::bigint), 0), sum(duration_ms)
            FROM understanding_chunk_calls WHERE agent_id = %s GROUP BY job_id
            """,
            (agent_id,),
        ).fetchall()
        checks = conn.execute(
            """
            SELECT check_key, count(*), coalesce(sum((usage_metadata->>'input_tokens')::bigint), 0),
                   coalesce(sum((usage_metadata->'input_token_details'->>'cache_read')::bigint), 0),
                   coalesce(sum((usage_metadata->>'output_tokens')::bigint), 0), sum(duration_ms)
            FROM understanding_group_calls WHERE agent_id = %s GROUP BY check_key
            """,
            (agent_id,),
        ).fetchall()
    return _costs(jobs), _costs(checks)
