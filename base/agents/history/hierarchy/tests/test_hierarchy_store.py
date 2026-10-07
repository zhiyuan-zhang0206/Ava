"""`base.agents.history.hierarchy.store` — the understanding-tree reads, against real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

from base.agents.history.hierarchy.store import load_generation_costs, load_nodes
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


def test_generation_costs_are_summed_per_chunk_job_and_per_grouping_check() -> None:
    db = Database.from_settings()
    usage = '{"input_tokens": 5, "output_tokens": 2, "input_token_details": {"cache_read": 3}}'
    with db.write_transaction() as conn:
        job = conn.execute(
            "INSERT INTO understanding_chunk_jobs"
            " (agent_id, compact_version, start_index, end_index, end_msg_id)"
            " VALUES (%s, 3, 8, 20, 'm20') RETURNING id",
            (AGENT_B,),
        ).fetchone()
        assert job is not None
        for rnd in (0, 1):  # the answer and one correction
            conn.execute(
                "INSERT INTO understanding_chunk_calls (job_id, agent_id, attempt, round, model,"
                " instruction, prefix_len, start_offset, usage_metadata, duration_ms)"
                " VALUES (%s, %s, 1, %s, 'm', 'i', 100, 8, %s::jsonb, 1500)",
                (job[0], AGENT_B, rnd, usage),
            )
        conn.execute(
            "INSERT INTO understanding_group_calls (agent_id, level, check_key, round, model,"
            " open_ids, request, usage_metadata, duration_ms)"
            " VALUES (%s, 1, 'ck-1', 0, 'm', '{1,2}', 'r', %s::jsonb, 2000)",
            (AGENT_B, usage),
        )
    jobs, checks = load_generation_costs(db, AGENT_B)
    cost = jobs[int(job[0])]
    assert (cost.calls, cost.input, cost.cache_read, cost.output, cost.seconds) == (
        2,
        10,
        6,
        4,
        3.0,
    )
    assert checks["ck-1"].calls == 1 and checks["ck-1"].seconds == 2.0


def test_a_node_names_the_job_and_the_check_that_wrote_it() -> None:
    db = Database.from_settings()
    with db.write_transaction() as conn:
        conn.execute(_INSERT_NODE, (AGENT_A + 10, 1, 0, 4, T0, T1, "leaf", None))
        conn.execute(
            "UPDATE understanding_nodes SET job_id = 41, check_key = 'ck' WHERE agent_id = %s",
            (AGENT_A + 10,),
        )
    (row,) = load_nodes(db, AGENT_A + 10)
    assert (row.job_id, row.check_key) == (41, "ck")


def test_a_node_an_older_releases_worker_wrote_is_not_read() -> None:
    """A mixed-version window can leave nodes with a bare-number engine version beside the new
    ones: only `chunk-*` / `group-*` nodes belong to this tree."""
    db = Database.from_settings()
    with db.write_transaction() as conn:
        conn.execute(_INSERT_NODE, (AGENT_A + 20, 1, 0, 4, T0, T1, "new", None))
        conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (%s, 1, 5, 9, 'k', 'old', 'h', 'i', 0, 'm', '0.3', '0.3', 1)",
            (AGENT_A + 20,),
        )
    assert [r.text for r in load_nodes(db, AGENT_A + 20)] == ["new"]
