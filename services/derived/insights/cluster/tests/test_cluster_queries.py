"""The cluster reads over a live database: curves, lanes and messages, and their routes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.db import Database, create_agent
from services.derived.insights.cluster import curves, lanes, messages
from services.derived.insights.cluster import router as cluster_router
from services.derived.insights.cluster.selection import load_tree

T0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=3)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def agent(conn: psycopg.Connection, spawner: str = "user", *, fork: int | None = None) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, status, fork_source_agent_id, fork_source_checkpoint_id) "
        "VALUES (%s, %s, 'idling', %s, %s)",
        (agent_id, spawner, fork, None if fork is None else "c"),
    )
    conn.commit()
    return agent_id


def usage(
    conn: psycopg.Connection, agent_id: int, minutes: float, cost: float | None, latency_ms: int = 0
) -> None:
    attrs: dict[str, object] = {"in_total": 100, "out_total": 10, "latency_ms": latency_ms}
    if cost is not None:
        attrs["cost_usd"] = cost
    conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), at(minutes), agent_id, json.dumps(attrs)),
    )
    conn.commit()


def send(
    conn: psycopg.Connection,
    sender: int,
    receiver: int,
    sent: float,
    claimed: float | None,
    *,
    content: str = "hi",
) -> int:
    """A message as `insert_inbound_message` records it: the inbound row, then the audit row
    whose `agent_id` is the receiver and `target_agent_id` the sender."""
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, source, created_at, claimed_at) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            receiver,
            content,
            f"agent:{sender}",
            at(sent),
            None if claimed is None else at(claimed),
        ),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
        "agent_id, target_agent_id, attributes) VALUES (%s, %s, 'm', 'p', 'send_message', 'info', "
        "%s, %s, %s, %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            at(sent),
            f"agent:{sender}",
            receiver,
            sender,
            json.dumps({"inbound_id": row[0], "content": content}),
        ),
    )
    conn.commit()
    return int(row[0])


def node(
    conn: psycopg.Connection, agent_id: int, depth: int, span: int, start: float, end: float
) -> None:
    conn.execute(
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts, "
        "segment_key, text, text_hash, input_hash, children_count, model, engine_version, "
        "prompt_version, schema_version) VALUES (%s, %s, %s, %s, %s, %s, 's', %s, 'h', 'i', 0, "
        "'m', 'e', 'p', 1)",
        (agent_id, depth, span, span, at(start), at(end), f"node {depth}.{span}"),
    )
    conn.commit()


@pytest.fixture
def family(db_conn: psycopg.Connection) -> tuple[int, int, int, int]:
    """A root, a child it spawned, a fork of the child, and an unrelated agent."""
    root = agent(db_conn)
    child = agent(db_conn, f"agent:{root}")
    forked = agent(db_conn, f"agent:{child}", fork=child)
    other = agent(db_conn)
    return root, child, forked, other


def test_the_tree_follows_spawn_and_fork_edges_from_the_root(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, forked, _ = family
    tree = load_tree(db_conn, root, "all")
    assert [(a.row.id, a.parent, a.kind) for a in tree] == [
        (root, None, "root"),
        (child, root, "spawn"),
        (forked, child, "fork"),
    ]
    assert [a.row.id for a in load_tree(db_conn, root, "spawn")] == [root, child]
    with pytest.raises(ValueError, match="unknown agent IDs"):
        load_tree(db_conn, 10**9, "all")


def test_curves_stack_cost_by_agent_and_count_active_agents(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, _, other = family
    usage(db_conn, root, 0.2, 0.5)
    usage(db_conn, root, 0.4, 0.25)
    usage(db_conn, child, 0.5, 1.0)
    usage(db_conn, child, 0.6, None)  # unpriced: a call, no cost
    usage(db_conn, child, 2.5, 2.0)  # next bucket
    usage(db_conn, other, 0.5, 9.0)  # not in the selection
    ids = [root, child]
    result = curves.read(db_conn, ids, at(0), at(10), 60)
    first, second = result.buckets
    assert first.ts == at(0)
    assert [(c.agent_id, c.calls, c.cost_usd) for c in first.costs] == [
        (root, 2, 0.75),
        (child, 2, 1.0),
    ]
    assert first.active_agents == 2
    assert [(c.agent_id, c.cost_usd) for c in second.costs] == [(child, 2.0)]
    assert second.active_agents == 1
    assert result.unpriced_calls == 1
    assert result.bucket_seconds == 60


def test_messages_count_per_bucket_with_queue_percentiles_of_claimed_rows(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, _, other = family
    send(db_conn, root, child, 0.1, 0.1 + 2 / 60)  # 2 s in the queue
    send(db_conn, root, child, 0.2, 0.2 + 4 / 60)  # 4 s
    send(db_conn, child, root, 0.3, None)  # never claimed: counted, no queue sample
    send(db_conn, other, child, 0.4, 0.5)  # sender outside the selection
    result = curves.read(db_conn, [root, child], at(0), at(10), 60)
    (bucket,) = result.buckets
    assert bucket.messages == 3
    assert bucket.queue_samples == 2
    assert bucket.queue_p50_seconds == pytest.approx(3.0, abs=0.01)
    assert bucket.queue_p95_seconds == pytest.approx(3.9, abs=0.01)
    assert bucket.active_agents == 0


def test_a_message_edge_runs_from_its_sender_to_its_receiver(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, _, other = family
    inbound = send(db_conn, root, child, 1, 1.5, content="x" * 500)
    send(db_conn, child, root, 2, None)
    send(db_conn, other, root, 3, 3.1)
    result = messages.read(db_conn, [root, child], at(0), at(10), limit=10)
    first, second = result.edges
    assert (first.inbound_id, first.sender, first.receiver) == (inbound, root, child)
    assert (first.sent_at, first.read_at) == (at(1), at(1.5))
    assert len(first.preview) == messages.PREVIEW_CHARS
    assert (second.sender, second.receiver, second.read_at) == (child, root, None)
    assert (result.total, result.truncated) == (2, False)


def test_messages_past_the_limit_are_counted_and_flagged(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, _, _ = family
    for minute in (1, 2, 3):
        send(db_conn, root, child, minute, minute + 0.1)
    result = messages.read(db_conn, [root, child], at(0), at(10), limit=2)
    assert (result.total, result.truncated, len(result.edges)) == (3, True, 2)


def test_lanes_carry_activity_bars_nodes_of_the_chosen_level_and_lifecycle_markers(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, forked, _ = family
    usage(db_conn, root, 1.0, 0.5, latency_ms=30_000)  # a request over [0.5, 1.0]
    usage(db_conn, root, 1.1, 0.5, latency_ms=1_000)  # follows within the merge distance
    usage(db_conn, root, 8.0, 0.5)
    for span, (start, end) in enumerate([(0, 2), (2, 4), (4, 9)]):
        node(db_conn, root, 1, span, start, end)
    node(db_conn, root, 2, 0, 0, 9)
    node(db_conn, child, 1, 0, 1, 2)
    db_conn.execute(
        "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
        "agent_id) VALUES (%s, %s, 'm', 'p', 'spawn', 'info', 'test', %s)",
        (uuid.uuid4().int % (1 << 62), at(0.1), child),
    )
    db_conn.commit()
    tree = load_tree(db_conn, root, "all")

    result = lanes.read(db_conn, tree, at(0), at(10), 5, level=1)
    by_id = {lane.agent_id: lane for lane in result.lanes}
    assert [lane.agent_id for lane in result.lanes] == [root, child, forked]
    assert (result.level, result.auto_level) == (1, False)
    assert [(lv.level, lv.nodes) for lv in result.levels] == [(1, 4), (2, 1)]
    assert [(n.start, n.end) for n in by_id[root].nodes] == [
        (at(0), at(2)),
        (at(2), at(4)),
        (at(4), at(9)),
    ]
    assert [(b.start, b.end, b.calls) for b in by_id[root].bars] == [
        (at(0.5), at(1.1), 2),
        (at(8.0), at(8.0), 1),
    ]
    assert by_id[root].calls == 3
    assert by_id[root].cost_usd == pytest.approx(1.5)
    assert by_id[child].events[0].kind == "spawn"
    assert by_id[forked].nodes == [] and by_id[forked].bars == []

    auto = lanes.read(db_conn, tree, at(0), at(10), 5, level=None)
    assert auto.auto_level and auto.level == 1  # 4 and 1 nodes against a target of 24: 4 is nearer


def test_nodes_outside_the_window_are_left_out(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, *_ = family
    node(db_conn, root, 1, 0, -30, -20)
    node(db_conn, root, 1, 1, 5, 6)
    result = lanes.read(db_conn, load_tree(db_conn, root, "self"), at(0), at(10), 5, level=None)
    assert [(n.start, n.end) for n in result.lanes[0].nodes] == [(at(5), at(6))]


def client() -> TestClient:
    app = FastAPI()
    app.state.db = Database.from_settings()
    app.include_router(cluster_router.router)
    return TestClient(app)


def window() -> dict[str, str]:
    return {"from": at(0).isoformat(), "to": at(10).isoformat()}


def test_the_routes_answer_over_the_same_selection(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, child, _, _ = family
    usage(db_conn, child, 1.0, 0.5)
    send(db_conn, root, child, 2, 2.5)
    http = client()
    params = {"root": root, **window()}
    curve = http.get("/api/insights/cluster/curves", params=params)
    assert curve.status_code == 200
    assert (
        curve.json()["agent_ids"] == sorted(curve.json()["agent_ids"])
        and len(curve.json()["agent_ids"]) == 3
    )
    assert curve.json()["window"]["from"]
    lane = http.get("/api/insights/cluster/lanes", params=params)
    assert [x["agent_id"] for x in lane.json()["lanes"]][:2] == [root, child]
    edges = http.get("/api/insights/cluster/messages", params=params).json()["edges"]
    assert [(e["sender"], e["receiver"]) for e in edges] == [(root, child)]


def test_the_routes_reject_a_bad_window_and_an_unknown_root(
    db_conn: psycopg.Connection, family: tuple[int, int, int, int]
) -> None:
    root, *_ = family
    http = client()
    naive = {"root": root, "from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00Z"}
    assert http.get("/api/insights/cluster/curves", params=naive).status_code == 422
    backwards = {"root": root, "from": at(10).isoformat(), "to": at(0).isoformat()}
    assert http.get("/api/insights/cluster/lanes", params=backwards).status_code == 422
    unknown = {"root": 10**9, **window()}
    assert http.get("/api/insights/cluster/messages", params=unknown).status_code == 404
    assert (
        http.get("/api/insights/cluster/curves", params={"root": 0, **window()}).status_code == 422
    )
