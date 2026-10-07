# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""`POST /api/agents/{id}/understanding/close`: plans the live segment's tail as one job."""

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.checkpoint import CheckpointReadError, FullHistory
from gateway.agents import understanding as close_module
from gateway.app import app


def _seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'test', 'idling', 'm')",
            (row[0],),
        )
    db_conn.commit()
    return int(row[0])


def _inbound(i: int) -> HumanMessage:
    return HumanMessage(content=f"m{i}", id=f"m{i}", additional_kwargs={"ava_msg_type": "inbound"})


def _history(count: int, *, segments: int = 1, open_call: bool = False) -> FullHistory:
    """`segments` segments of `count` inbound messages each (head: one SystemMessage)."""
    body: list[BaseMessage] = [_inbound(i) for i in range(count * segments)]
    if open_call:
        body.append(
            AIMessage(content="", id="open", tool_calls=[{"name": "x", "id": "t", "args": {}}])
        )
    starts = tuple(i * count for i in range(segments))
    return FullHistory(body, tuple(SystemMessage(content="head") for _ in range(segments)), starts)


def _jobs(conn: psycopg.Connection, agent_id: int) -> list[tuple[object, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compact_version, start_index, end_index, end_msg_id, status,"
            " boundary_checkpoint_id FROM understanding_chunk_jobs WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def _close(agent_id: int) -> dict:
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{agent_id}/understanding/close")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_new_segment_is_closed_from_past_its_head_to_its_last_request(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(5))
    body = _close(tid)
    # request = [head, m0..m4]: the SystemMessage head, 5 messages; the first message past
    # the head (an inbound) is index 1, the request ends at 6.
    assert body["status"] == "enqueued" and body["job_id"] is not None
    assert (body["compact_version"], body["start_index"], body["end_index"]) == (0, 1, 6)
    assert _jobs(db_conn, tid) == [(0, 1, 6, "m4", "pending", None)]


def test_a_trailing_open_tool_call_is_left_out(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(
        close_module, "load_checkpoint_history_full", lambda *_a: _history(3, open_call=True)
    )
    assert _close(tid)["end_index"] == 4  # head + m0..m2; the unanswered call is not sendable


def test_the_newest_segment_is_the_one_closed(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(
        close_module, "load_checkpoint_history_full", lambda *_a: _history(4, segments=3)
    )
    body = _close(tid)
    # The segment has no job yet: the version is the next one after the agent's newest job (none
    # here), not the segment's index; the stretch is the newest segment's.
    assert body["compact_version"] == 0 and (body["start_index"], body["end_index"]) == (1, 5)


def _insert_job(
    conn: psycopg.Connection,
    agent_id: int,
    start: int,
    end: int,
    status: str,
    *,
    version: int = 0,
) -> None:
    """A job of the live segment of `_history(n)`: its request is [head, m0, m1, ...], so the
    message at request index `end - 1` is `m{end - 2}`."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO understanding_chunk_jobs (agent_id, compact_version, start_index, end_index,"
            " end_msg_id, status) VALUES (%s, %s, %s, %s, %s, %s)",
            (agent_id, version, start, end, f"m{end - 2}", status),
        )
    conn.commit()


def test_the_stretch_starts_where_the_last_job_ended(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(10))
    _insert_job(db_conn, tid, 1, 4, "done")
    body = _close(tid)
    assert (body["status"], body["start_index"], body["end_index"]) == ("enqueued", 4, 11)


def test_nothing_past_the_cut_is_empty_and_writes_nothing(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(5))
    _insert_job(db_conn, tid, 1, 6, "done")
    body = _close(tid)
    assert body["status"] == "empty" and body["job_id"] is None
    assert [j[4] for j in _jobs(db_conn, tid)] == ["done"]


def test_an_active_job_blocks_a_second_close(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(5))
    first = _close(tid)
    assert first["status"] == "enqueued"
    second = _close(tid)
    assert second["status"] == "active_job" and second["job_id"] == first["job_id"]
    assert len(_jobs(db_conn, tid)) == 1


def test_a_failed_close_is_retried_by_the_next_one(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(5))
    first = _close(tid)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE understanding_chunk_jobs SET status = 'failed', attempts = 3, error = 'x'"
        )
    db_conn.commit()
    again = _close(tid)
    assert again["status"] == "enqueued" and again["job_id"] == first["job_id"]
    assert _jobs(db_conn, tid) == [(0, 1, 6, "m4", "pending", None)]


def test_an_unknown_agent_is_404(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/agents/9999/understanding/close")
    assert resp.status_code == 404


def test_an_unreadable_history_is_503(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed_agent(db_conn)

    def broken(*_a: object) -> FullHistory:
        raise CheckpointReadError("db blip")

    monkeypatch.setattr(close_module, "load_checkpoint_history_full", broken)
    with TestClient(app) as client:
        assert client.post(f"/api/agents/{tid}/understanding/close").status_code == 503


def test_the_close_reuses_the_version_of_the_live_segments_own_jobs(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compaction whose boundary stamp failed leaves the producers' `compact_version` ahead of
    the segment count. The close finds the segment's jobs by message id and joins their version
    instead of deriving one from the segment index (which opened a second, overlapping chain)."""
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(10))
    _insert_job(db_conn, tid, 1, 4, "done", version=5)
    body = _close(tid)
    assert (body["compact_version"], body["start_index"], body["end_index"]) == (5, 4, 11)


def test_the_close_never_starts_before_the_end_of_an_existing_level_one_node(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nodes of one level must not overlap. The producers' next size cut (or a replay) may already
    have nodes past the last job: the close takes up after them."""
    tid = _seed_agent(db_conn)
    monkeypatch.setattr(close_module, "load_checkpoint_history_full", lambda *_a: _history(10))
    _insert_job(db_conn, tid, 1, 4, "done")
    with db_conn.cursor() as cur:  # request index 6 is stitched message 5 (head at stitched -1)
        cur.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (%s, 1, 3, 5, 'k', 't', 'h', 'i', 0, 'm', 'e', 'p', 1)",
            (tid,),
        )
    db_conn.commit()
    body = _close(tid)
    assert (body["status"], body["start_index"], body["end_index"]) == ("enqueued", 7, 11)
