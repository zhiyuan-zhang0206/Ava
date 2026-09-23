"""Persistence for the understanding tree — the `understanding_nodes` table.

Write path (`write_tree`): one upsert per materialized node keyed by the
deterministic span identity `(agent_id, depth, span_start, span_end)`. An
identical text is a no-op rewrite; a changed text (a regeneration after an
engine or prompt change) overwrites in place, keeping one row per node.
Parent links are resolved after the upserts from children spans, so the order
of nodes in one run never matters. A rebuild reconciles the table to its own
partition: rows it did not reproduce are removed when they overlap a
reproduced span at the same level (an earlier cut of a stretch being re-cut —
the provisional tail re-splits as history grows), and rows outside every
reproduced span survive (a compact-driven pass seals no tail, so a pending
stretch's earlier rows are still its best coverage).

Read paths:
- `load_known_texts(agent_id)` -> `{input_hash: text}`, the generation reuse
  cache that makes a rerun over unchanged history cost zero model calls;
- `load_window_nodes(agent_id, start, end)`, nodes intersecting a window for
  the run-timeline serving merge;
- `load_coverage_extent(agent_id)`, the agent-wide `(min start, max end)`
  sealed extent the pending-placeholder cut reads.

Identity note: span identity is stable because compaction boundaries are never
trimmed (#1125) — the stitched full history is append-only, so message indices
never shift. Nodes without timestamps never match a window query (NULL
comparison) — they stay stored, just unservable until their time is known.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection

from shared.agents.history.hierarchy import ENGINE_VERSION, PROMPT_VERSION
from shared.agents.history.hierarchy.nodes import MaterializedNode
from shared.db import pool
from shared.db_transaction import write_transaction

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
def _read_connection() -> Generator[Connection[Any]]:
    """Borrow one short-lived autocommit read connection (checkpoint.py pattern)."""
    db_pool = pool(autocommit=True)
    try:
        with db_pool.connection() as conn:
            yield conn
    finally:
        db_pool.close()


def _parse_ts(value: str) -> datetime | None:
    """`ava_created_at` ISO string -> aware datetime; "" (legacy) -> NULL."""
    if not value:
        return None
    return datetime.fromisoformat(value)


def write_tree(agent_id: int, nodes: Sequence[MaterializedNode], *, model: str) -> int:
    """Upsert every node of one run; returns the number of rows written.

    Alias rows carry no model (no generation happened at the node itself) —
    their text is the child's, recorded so the serving merge can show the full
    chain.
    """
    if not nodes:
        return 0
    with write_transaction() as conn:
        ids: dict[tuple[int, int, int], int] = {}
        for node in nodes:
            cursor = conn.execute(
                """
                INSERT INTO understanding_nodes (
                    agent_id, depth, span_start, span_end, start_ts, end_ts,
                    segment_key, text, text_hash, input_hash, children_count,
                    model, engine_version, prompt_version, schema_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, depth, span_start, span_end) DO UPDATE SET
                    start_ts = EXCLUDED.start_ts,
                    end_ts = EXCLUDED.end_ts,
                    segment_key = EXCLUDED.segment_key,
                    text = EXCLUDED.text,
                    text_hash = EXCLUDED.text_hash,
                    input_hash = EXCLUDED.input_hash,
                    children_count = EXCLUDED.children_count,
                    model = EXCLUDED.model,
                    engine_version = EXCLUDED.engine_version,
                    prompt_version = EXCLUDED.prompt_version,
                    schema_version = EXCLUDED.schema_version,
                    updated_at = now()
                RETURNING id
                """,
                (
                    agent_id,
                    node.level,
                    node.span[0],
                    node.span[1],
                    _parse_ts(node.at[0]),
                    _parse_ts(node.at[1]),
                    node.trigger,
                    node.text,
                    node.text_hash,
                    node.input_hash,
                    len(node.children),
                    "" if node.kind == "alias" else model,
                    ENGINE_VERSION,
                    PROMPT_VERSION,
                    SCHEMA_VERSION,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"understanding_nodes upsert returned no id for {node.nid}")
            ids[(node.level, node.span[0], node.span[1])] = int(row[0])
        linked = 0
        for node in nodes:
            if node.level < 2:
                continue  # a leaf's children are blocks, not rows
            parent_id = ids[(node.level, node.span[0], node.span[1])]
            for child_span in node.children_spans:
                cursor = conn.execute(
                    """
                    UPDATE understanding_nodes SET parent_id = %s
                    WHERE agent_id = %s AND depth = %s AND span_start = %s AND span_end = %s
                    """,
                    (parent_id, agent_id, node.level - 1, child_span[0], child_span[1]),
                )
                linked += cursor.rowcount
        if linked != sum(len(n.children_spans) for n in nodes if n.level >= 2):
            raise RuntimeError(
                f"parent linkage resolved {linked} rows for "
                f"{sum(len(n.children_spans) for n in nodes if n.level >= 2)} children — "
                "the tree and the stored rows disagree"
            )
        # Reconcile: a rebuild after history grew re-cuts only the provisional
        # tail (compact-sealed stretches reproduce identically). Rows this run
        # did not reproduce but that overlap a reproduced span at the same
        # level are an earlier cut of a re-cut stretch — removed, so storage
        # mirrors the current partition. An unreproduced row with no reproduced
        # overlap stays: this pass left its stretch pending (a compact-driven
        # pass seals no tail), and the row is still that region's coverage.
        spans_by_level: dict[int, list[tuple[int, int]]] = {}
        for node in nodes:
            spans_by_level.setdefault(node.level, []).append(node.span)
        reproduced = {(node.level, node.span[0], node.span[1]) for node in nodes}
        stale_ids: list[int] = []
        stored = conn.execute(
            "SELECT id, depth, span_start, span_end FROM understanding_nodes WHERE agent_id = %s",
            (agent_id,),
        ).fetchall()
        for row_id, depth, span_start, span_end in stored:
            if (depth, span_start, span_end) in reproduced:
                continue
            if any(
                start <= span_end and end >= span_start
                for start, end in spans_by_level.get(depth, ())
            ):
                stale_ids.append(row_id)
        if stale_ids:
            conn.execute("DELETE FROM understanding_nodes WHERE id = ANY(%s)", (stale_ids,))
    return len(nodes)


def load_known_texts(agent_id: int) -> dict[str, str]:
    """The generation reuse cache: `input_hash -> text` for one agent."""
    with _read_connection() as conn:
        rows = conn.execute(
            "SELECT input_hash, text FROM understanding_nodes WHERE agent_id = %s",
            (agent_id,),
        ).fetchall()
    return {str(input_hash): str(text) for input_hash, text in rows}


def load_coverage_extent(agent_id: int) -> tuple[datetime, datetime] | None:
    """The agent's global sealed coverage -- `(min start, max end)` of timed nodes.

    The pending-placeholder computation cuts activity at this extent's start
    (display never promises generation for never-sealed history), so the read
    is agent-wide, not window-limited. `None` when no node carries timestamps.
    """
    with _read_connection() as conn:
        row = conn.execute(
            "SELECT min(start_ts), max(end_ts) FROM understanding_nodes WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return row[0], row[1]


def load_window_nodes(agent_id: int, start: datetime, end: datetime) -> list[StoredNode]:
    """Nodes intersecting `[start, end]` (inclusive), ordered by depth then start.

    An inclusive intersection: a node counts when any part of it falls in the
    window. Callers needing exact coverage compose the tree from the returned
    set. Timestamps must be timezone-aware; rows whose times are unknown
    (legacy messages) never match.
    """
    with _read_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, depth, span_start, span_end, start_ts, end_ts, text, parent_id,
                   engine_version, prompt_version
            FROM understanding_nodes
            WHERE agent_id = %s AND start_ts <= %s AND end_ts >= %s
            ORDER BY depth, start_ts
            """,
            (agent_id, end, start),
        ).fetchall()
    return [StoredNode(*row) for row in rows]
