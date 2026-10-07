"""`base.agents.history.hierarchy.store` — the understanding-tree reads, against real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

from base.agents.history.hierarchy.store import load_call_records, load_nodes
from base.db import Database

AGENT_A = 990_128_901  # node reads
AGENT_B = 990_128_902  # call records

T0 = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 12, 4, 5, tzinfo=UTC)

_INSERT_NODE = (
    "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
    " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
    " prompt_version, schema_version, parent_id)"
    " VALUES (%s, %s, %s, %s, %s, %s, 'k', %s, 'th', 'ih', 0, 'm', 'chunk-0.2', 'chunk-0.12', 1, %s)"
    " RETURNING id"
)


def test_load_nodes_returns_every_node_ordered_by_depth_then_position() -> None:
    db = Database.from_settings()
    with db.write_transaction() as conn:
        parent = conn.execute(_INSERT_NODE, (AGENT_A, 2, 0, 9, T0, T1, "parent", None)).fetchone()
        assert parent is not None
        conn.execute(_INSERT_NODE, (AGENT_A, 1, 5, 9, T0, T1, "second", parent[0]))
        conn.execute(_INSERT_NODE, (AGENT_A, 1, 0, 4, T0, None, "first", parent[0]))
    rows = load_nodes(db, AGENT_A)
    assert [(r.depth, r.span_start, r.text) for r in rows] == [
        (1, 0, "first"),
        (1, 5, "second"),
        (2, 0, "parent"),
    ]
    assert rows[0].parent_id == rows[2].id and rows[2].parent_id is None
    assert rows[0].end_ts is None and rows[0].start_ts == T0  # an untimed end is kept as stored
    assert (rows[0].engine_version, rows[0].prompt_version) == ("chunk-0.2", "chunk-0.12")


def test_load_call_records_joins_each_call_to_its_jobs_segment() -> None:
    db = Database.from_settings()
    with db.write_transaction() as conn:
        job = conn.execute(
            "INSERT INTO understanding_chunk_jobs"
            " (agent_id, compact_version, start_index, end_index, end_msg_id)"
            " VALUES (%s, 3, 8, 20, 'm20') RETURNING id",
            (AGENT_B,),
        ).fetchone()
        assert job is not None
        conn.execute(
            "INSERT INTO understanding_chunk_calls (job_id, agent_id, attempt, round, model,"
            " instruction, prefix_len, start_offset, usage_metadata, duration_ms)"
            " VALUES (%s, %s, 1, 0, 'm', 'i', 100, 8, '{\"input_tokens\": 5}'::jsonb, 1500)",
            (job[0], AGENT_B),
        )
    (record,) = load_call_records(db, AGENT_B)
    assert (record.compact_version, record.start_offset, record.prefix_len) == (3, 8, 100)
    assert record.usage_metadata == {"input_tokens": 5} and record.duration_ms == 1500.0
