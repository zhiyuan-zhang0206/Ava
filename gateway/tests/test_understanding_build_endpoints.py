# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""`GET /sessions`, `POST /understanding/build` and `GET /understanding/builds/{id}`."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from psycopg import sql

from base.agents.history.checkpoint import CheckpointReadError, FullHistory
from base.config import settings
from gateway.agents import understanding as module
from gateway.app import app

_T0 = datetime(2026, 10, 5, tzinfo=UTC)
_MODEL = "deepseek-v4-flash"


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


def _turns(tag: str, count: int, *, first_hour: int) -> list[BaseMessage]:
    """`count` turns (an inbound, then an AI turn whose input grows by 400 tokens), one per minute."""
    out: list[BaseMessage] = []
    for i in range(count):
        stamp = (_T0 + timedelta(hours=first_hour, minutes=i)).isoformat()
        out.append(
            HumanMessage(
                content=f"{tag}{i}",
                id=f"{tag}h{i}",
                additional_kwargs={
                    "ava_msg_type": "inbound",
                    "ava_source": "user",
                    "ava_created_at": stamp,
                },
            )
        )
        tokens = 2000 + 400 * i
        out.append(
            AIMessage(
                content="ok",
                id=f"{tag}a{i}",
                additional_kwargs={"ava_created_at": stamp},
                usage_metadata={
                    "input_tokens": tokens,
                    "output_tokens": 10,
                    "total_tokens": tokens + 10,
                },
            )
        )
    return out


def _history(*counts: int) -> FullHistory:
    """One session per entry (`counts` turns each, three hours apart); a SystemMessage head each."""
    body: list[BaseMessage] = []
    starts: list[int] = []
    for k, count in enumerate(counts):
        starts.append(len(body))
        body.extend(_turns(f"s{k}", count, first_hour=k * 3))
    return FullHistory(body, tuple(SystemMessage(content="head") for _ in counts), tuple(starts))


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Session 1 (cp-a, 12 messages) and session 2 (in progress, 6 messages); a small chunk size."""
    state: dict[str, Any] = {"history": _history(6, 3), "boundaries": ["cp-a"]}
    monkeypatch.setattr(module, "load_checkpoint_history_full", lambda *_a: state["history"])
    monkeypatch.setattr(
        module,
        "list_compact_boundary_checkpoint_ids",
        lambda *_a: list(reversed(state["boundaries"])),
    )
    monkeypatch.setattr(module, "build_model", lambda *_a: _MODEL)
    monkeypatch.setattr(module, "chunk_size", lambda *_a: 1000)
    return state


def _client() -> TestClient:
    return TestClient(app)


def _build(client: TestClient, agent_id: int, **body: Any) -> dict[str, Any]:
    resp = client.post(f"/api/agents/{agent_id}/understanding/build", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _node(conn: psycopg.Connection, agent_id: int, first: int, last: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
            " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
            " prompt_version, schema_version)"
            " VALUES (%s, 1, %s, %s, now(), now(), 'k', 't', 'h', %s, 0, 'm', 'chunk-0.2', 'p', 1)",
            (agent_id, first, last, f"i{first}-{last}"),
        )
    conn.commit()


def _jobs(conn: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compact_version, start_index, end_index, end_msg_id, status,"
            " boundary_checkpoint_id FROM understanding_chunk_jobs WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def test_the_sessions_list_numbers_times_tokens_coverage_and_estimate(
    db_conn: psycopg.Connection,
) -> None:
    agent = _seed_agent(db_conn)
    _node(db_conn, agent, 0, 6)
    with _client() as client:
        resp = client.get(f"/api/agents/{agent}/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == _MODEL and body["understanding_enabled"] is False
    assert "cold" in body["cost_basis"].lower()
    first, live = body["sessions"]
    assert (first["number"], first["boundary_checkpoint_id"], first["messages"]) == (1, "cp-a", 12)
    assert (live["number"], live["boundary_checkpoint_id"], live["messages"]) == (2, None, 6)
    assert first["peak_input_tokens"] == 4000 and live["peak_input_tokens"] == 2800
    assert first["start"].startswith("2026-10-05T00:00:00")
    assert first["end"].startswith("2026-10-05T00:05:00")
    assert live["start"].startswith("2026-10-05T03:00:00")
    # Session 1: the first chunk (messages 0..6) is described, the closing one is not.
    assert first["coverage"] == {
        "status": "partial",
        "ratio": 7 / 12,
        "covered_messages": 7,
        "total_messages": 12,
    }
    assert first["estimate"]["jobs"] == 1  # only the uncovered closing chunk
    assert live["coverage"]["status"] == "none" and live["estimate"]["jobs"] == 1
    assert live["estimate"]["cost_usd"] > 0


def test_a_fully_described_session_is_full_and_costs_nothing(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    _node(db_conn, agent, 0, 11)
    with _client() as client:
        first = client.get(f"/api/agents/{agent}/sessions").json()["sessions"][0]
    assert first["coverage"]["status"] == "full" and first["coverage"]["ratio"] == 1.0
    assert first["estimate"] == {"jobs": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def test_a_dry_run_plans_and_prices_but_writes_nothing(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        body = _build(client, agent, sessions=[1, 2], dry_run=True)
    assert body["dry_run"] is True and body["build_id"] is None and body["rebuild_id"] is None
    assert [(j["session"], j["start_index"], j["end_index"], j["state"]) for j in body["jobs"]] == [
        (1, 1, 8, "planned"),
        (1, 8, 13, "planned"),
        (2, 1, 7, "planned"),
    ]
    assert all(j["job_id"] is None for j in body["jobs"])
    assert body["estimate"]["jobs"] == 3 and body["estimate"]["cost_usd"] > 0
    assert body["queued"] == 0
    for table in ("understanding_chunk_jobs", "understanding_rebuilds", "understanding_builds"):
        assert _count(db_conn, table) == 0


def test_a_build_enqueues_jobs_skips_what_is_covered_and_queues_one_rebuild(
    db_conn: psycopg.Connection,
) -> None:
    agent = _seed_agent(db_conn)
    _node(db_conn, agent, 0, 6)  # session 1's first chunk is described
    with _client() as client:
        body = _build(client, agent, sessions=[1, 2])
    assert body["dry_run"] is False and body["understanding_enabled"] is False
    assert (body["queued"], body["merged"]) == (2, 0)
    assert [
        (j["session"], j["first_message"], j["last_message"], j["state"]) for j in body["jobs"]
    ] == [
        (1, 7, 11, "enqueued"),
        (2, 12, 17, "enqueued"),
    ]
    # Jobs are plain chunk jobs: a closed session names its boundary checkpoint, the live one none;
    # the build is queued although the feature switch is off.
    assert _jobs(db_conn, agent) == [
        (0, 8, 13, "s0a5", "pending", "cp-a"),
        (1, 1, 7, "s1a2", "pending", None),
    ]
    with db_conn.cursor() as cur:
        cur.execute("SELECT agent_id, status FROM understanding_rebuilds")
        assert cur.fetchall() == [(agent, "pending")]
        cur.execute("SELECT id, sessions, jobs, rebuild_id FROM understanding_builds")
        ((build_id, sessions, jobs, rebuild_id),) = cur.fetchall()
    assert (build_id, sessions, rebuild_id) == (body["build_id"], [1, 2], body["rebuild_id"])
    assert [j["session"] for j in jobs] == [1, 2]


def test_a_second_build_merges_into_live_jobs_and_the_pending_rebuild(
    db_conn: psycopg.Connection,
) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        first = _build(client, agent, sessions=[1])
        second = _build(client, agent, sessions=[1, 2])
    assert (second["queued"], second["merged"]) == (1, 2)
    assert second["rebuild_id"] == first["rebuild_id"] and second["build_id"] != first["build_id"]
    assert len(_jobs(db_conn, agent)) == 3 and _count(db_conn, "understanding_rebuilds") == 1
    assert _count(db_conn, "understanding_builds") == 2
    shared = [j["job_id"] for j in second["jobs"] if j["state"] == "merged"]
    assert shared == [j["job_id"] for j in first["jobs"]]


def test_concurrent_builds_of_one_agent_merge_into_one_rebuild(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client, ThreadPoolExecutor(max_workers=4) as pool:
        bodies = list(pool.map(lambda _i: _build(client, agent, sessions=[1, 2]), range(4)))
    assert len({b["rebuild_id"] for b in bodies}) == 1
    assert len(_jobs(db_conn, agent)) == 3 and _count(db_conn, "understanding_rebuilds") == 1
    assert sum(b["queued"] for b in bodies) == 3  # each job newly queued by exactly one build
    assert len({b["build_id"] for b in bodies}) == 4


def test_a_running_rebuild_is_not_joined_a_pending_one_is_added(
    db_conn: psycopg.Connection,
) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        first = _build(client, agent, sessions=[1])
        with db_conn.cursor() as cur:
            cur.execute("UPDATE understanding_rebuilds SET status = 'running'")
        db_conn.commit()
        second = _build(client, agent, sessions=[2])
    assert second["rebuild_id"] != first["rebuild_id"]


def test_an_ended_job_of_the_same_stretch_is_taken_up_again(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        _build(client, agent, sessions=[2])
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE understanding_chunk_jobs SET status = 'failed', attempts = 3, error = 'x'"
            )
        db_conn.commit()
        again = _build(client, agent, sessions=[2])
    assert (again["queued"], again["merged"]) == (1, 0)
    assert _jobs(db_conn, agent) == [(1, 1, 7, "s1a2", "pending", None)]


def test_a_time_range_selects_the_sessions_it_intersects(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        late = _build(client, agent, dry_run=True, **{"from": "2026-10-05T02:00:00+00:00"})
        early = _build(client, agent, dry_run=True, to="2026-10-05T01:00:00Z")
        both = _build(
            client,
            agent,
            dry_run=True,
            **{"from": "2026-10-05T00:03:00Z", "to": "2026-10-05T03:01:00Z"},
        )
    assert (late["sessions"], early["sessions"], both["sessions"]) == ([2], [1], [1, 2])


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"sessions": []},
        {"sessions": [1], "from": "2026-10-05T00:00:00Z"},
        {"sessions": [3]},
        {"sessions": [0]},
        {"from": "2026-10-05T00:00:00"},
        {"from": "2027-01-01T00:00:00Z"},
    ],
)
def test_a_bad_selection_is_422_and_writes_nothing(
    db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        resp = client.post(f"/api/agents/{agent}/understanding/build", json=body)
    assert resp.status_code == 422, resp.text
    assert (
        _count(db_conn, "understanding_chunk_jobs") == _count(db_conn, "understanding_builds") == 0
    )


def test_the_switch_does_not_change_a_dry_run_and_a_build_is_queued_either_way(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        off = _build(client, agent, sessions=[2], dry_run=True)
        monkeypatch.setattr(settings.agent, "understanding_enabled", True)
        on = _build(client, agent, sessions=[2], dry_run=True)
    assert (off["understanding_enabled"], on["understanding_enabled"]) == (False, True)
    assert off["jobs"] == on["jobs"] and off["estimate"] == on["estimate"]


def test_the_progress_of_a_build_follows_its_jobs_then_its_rebuild(
    db_conn: psycopg.Connection,
) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        built = _build(client, agent, sessions=[2])
        url = f"/api/agents/{agent}/understanding/builds/{built['build_id']}"
        queued = client.get(url).json()
        assert queued["phase"] == "chunks" and queued["sessions"] == [2]
        assert [(j["session"], j["status"], j["calls"]) for j in queued["jobs"]] == [
            (2, "pending", 0)
        ]
        assert queued["rebuild"]["status"] == "pending" and queued["cost_usd"] == 0.0

        job_id = built["jobs"][0]["job_id"]
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE understanding_chunk_jobs SET status = 'done' WHERE id = %s", (job_id,)
            )
            cur.execute(
                "INSERT INTO understanding_chunk_calls (job_id, agent_id, attempt, round, model,"
                " instruction, prefix_len, start_offset, usage_metadata, duration_ms)"
                " VALUES (%s, %s, 1, 0, %s, 'i', 7, 1,"
                ' \'{"input_tokens": 100000, "output_tokens": 5000,'
                ' "input_token_details": {"cache_read": 20000}}\'::jsonb, 2000)',
                (job_id, agent, _MODEL),
            )
        db_conn.commit()
        waiting = client.get(url).json()
        assert waiting["phase"] == "rebuild_pending"
        (job,) = waiting["jobs"]
        assert (job["input_tokens"], job["cache_read_tokens"], job["output_tokens"]) == (
            100000,
            20000,
            5000,
        )
        assert job["seconds"] == 2.0
        assert job["cost_usd"] == pytest.approx((80000 * 0.15 + 20000 * 0.003 + 5000 * 0.6) / 1e6)

        with db_conn.cursor() as cur:
            cur.execute("UPDATE understanding_rebuilds SET status = 'done', leaves = 4")
        db_conn.commit()
        done = client.get(url).json()
    assert done["phase"] == "done" and done["rebuild"]["leaves"] == 4
    assert done["cost_usd"] == pytest.approx(job["cost_usd"])


def test_a_build_of_another_agent_or_an_unknown_one_is_404(db_conn: psycopg.Connection) -> None:
    agent, other = _seed_agent(db_conn), _seed_agent(db_conn)
    with _client() as client:
        built = _build(client, agent, sessions=[1])
        assert (
            client.get(f"/api/agents/{other}/understanding/builds/{built['build_id']}").status_code
            == 404
        )
        assert client.get(f"/api/agents/{agent}/understanding/builds/99999").status_code == 404


def test_an_unknown_agent_is_404_everywhere(db_conn: psycopg.Connection) -> None:
    with _client() as client:
        assert client.get("/api/agents/9999/sessions").status_code == 404
        assert (
            client.post("/api/agents/9999/understanding/build", json={"sessions": [1]}).status_code
            == 404
        )
        assert client.get("/api/agents/9999/understanding/builds/1").status_code == 404


def test_an_unreadable_history_is_503(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _seed_agent(db_conn)

    def broken(*_a: object) -> FullHistory:
        raise CheckpointReadError("db blip")

    monkeypatch.setattr(module, "load_checkpoint_history_full", broken)
    with _client() as client:
        assert client.get(f"/api/agents/{agent}/sessions").status_code == 503
        resp = client.post(f"/api/agents/{agent}/understanding/build", json={"sessions": [1]})
        assert resp.status_code == 503


def test_a_history_whose_boundaries_do_not_line_up_is_409(
    db_conn: psycopg.Connection, _seams: dict[str, Any]
) -> None:
    agent = _seed_agent(db_conn)
    _seams["boundaries"] = ["cp-a", "cp-b", "cp-c"]
    with _client() as client:
        assert client.get(f"/api/agents/{agent}/sessions").status_code == 409


def test_an_agent_without_history_has_no_sessions(
    db_conn: psycopg.Connection, _seams: dict[str, Any]
) -> None:
    agent = _seed_agent(db_conn)
    _seams["history"], _seams["boundaries"] = FullHistory([], (), ()), []
    with _client() as client:
        assert client.get(f"/api/agents/{agent}/sessions").json()["sessions"] == []
        resp = client.post(f"/api/agents/{agent}/understanding/build", json={"sessions": [1]})
    assert resp.status_code == 422


def test_the_close_endpoint_is_gone(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    with _client() as client:
        assert client.post(f"/api/agents/{agent}/understanding/close").status_code in (404, 405)


def test_a_process_without_the_agent_domain_reads_the_switch_and_chunk_size_from_the_env_file(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway profile does not construct the agent config domain: the cluster's values are
    read from the unit's `.env`, the field defaults when it does not set them."""
    agent = _seed_agent(db_conn)
    monkeypatch.setattr(module, "settings", SimpleNamespace(has_domain=lambda _name: False))
    file_values = {"AVA_UNDERSTANDING_ENABLED": "true", "AVA_UNDERSTANDING_CHUNK_RATIO": "0.25"}
    monkeypatch.setattr(module, "read_env_aliases", lambda: file_values)
    with _client() as client:
        body = _build(client, agent, sessions=[1], dry_run=True)
    assert body["understanding_enabled"] is True
    assert module.chunk_ratio() == 0.25

    file_values.clear()
    assert module.feature_enabled() is False
    assert module.chunk_ratio() == 0.5
