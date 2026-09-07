"""Integration test for `POST /api/agents/{id}/compact` in gateway/app.py.

Cross-module contract: `/compact framework` / `/compact agent` slash command hits this
endpoint and lets the kernel loop take over — web can only write pending, must never run
LLM or graph itself. Historically there was a hidden build_graph dependency bug here, which
unit tests and manual UI testing didn't catch. Here we use FastAPI TestClient to start the real app + real ava_test lib,
run end-to-end and assert whether inbound_messages table was correctly written.

No mocking DB — conftest already switched `settings.data_plane.db_url` to `ava_test`, lifespan
runs ConnectionPool connected to it as well.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.agents import AgentStatus
from shared.machine import machine_name


def _seed_agent(db_conn: psycopg.Connection, status: str = "idling") -> int:
    """Directly insert agents + agents_meta rows in DB, no session launch triggered. machine set to
    local machine name, mimics real spawn path (auto-resurrect decides local execution or
    forward based on machine; default 'unknown' would be considered an unreachable remote home machine)."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'test', %s, %s)",
            (new_id, status, machine_name()),
        )
    db_conn.commit()
    return new_id


def _pending_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def _status(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_compact_endpoint_inserts_compact_request_kind(db_conn: psycopg.Connection) -> None:
    """New design (Step 2 cleanup): mode no longer branches — framework / agent both INSERT
    kind='compact_request', claim Node runs backend LLM to generate summary replacement. Mode
    query parameter kept only for compatibility with old frontend, actually ignored. An alive agent does not trigger
    auto-resurrect, just leaves a compact_request."""
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{tid}/compact?mode=framework")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "framework", "agent_id": tid, "status": "enqueued"}
    assert _pending_rows(db_conn, tid) == [("compact_request", "pending")]


def test_compact_endpoint_ignores_mode_param(db_conn: psycopg.Connection) -> None:
    """mode=agent and mode=framework take the same path — response returns mode field for compat
    with old frontend, but inbound kind is always compact_request."""
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{tid}/compact?mode=agent")
    assert resp.status_code == 200
    assert resp.json()["status"] == "enqueued"
    assert _pending_rows(db_conn, tid) == [("compact_request", "pending")]


def test_compact_passes_inserted_id_and_kind_to_guarded_resurrect(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable compact row itself is the final CAS evidence; the route
    cannot call the unguarded explicit resurrection path."""
    import gateway.routers.agents_lifecycle as lifecycle

    tid = _seed_agent(db_conn)
    calls: list[tuple[int, int | None, str | None]] = []

    async def _resurrect(
        agent_id: int,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: str | None = None,
    ) -> AgentStatus:
        calls.append((agent_id, trigger_inbound_id, trigger_inbound_kind))
        return AgentStatus.IDLING

    monkeypatch.setattr(lifecycle._ops, "resurrect_if_terminated", _resurrect)
    with TestClient(app) as client:
        assert client.post(f"/api/agents/{tid}/compact").status_code == 200
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_request'",
            (tid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert calls == [(tid, row[0], "compact_request")]


def test_compact_terminated_agent_auto_resurrects(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compact targeting a terminated agent auto-resurrects it (shared with the
    chat path) so the compaction actually runs instead of leaving a compact_request
    row pending forever. The resurrect inserts its own newer 'resurrect' inbound and
    flips status terminated -> idling; the claim node's recency routing then lets
    the resurrect win while the compact_request still applies."""

    tid = _seed_agent(db_conn, status="terminated")
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{tid}/compact")
    assert resp.status_code == 200
    assert resp.json()["status"] == "enqueued"
    # compact_request first, then the resurrect the auto-resurrect appended.
    assert _pending_rows(db_conn, tid) == [
        ("compact_request", "pending"),
        ("resurrect", "pending"),
    ]
    # terminated -> idling: a fresh process is being launched (session guarded in tests).
    assert _status(db_conn, tid) == "idling"


def test_404_when_thread_not_exists(db_conn: psycopg.Connection) -> None:
    # conftest already TRUNCATE, no agent 9999
    with TestClient(app) as client:
        resp = client.post("/api/agents/9999/compact?mode=framework")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]
    assert _pending_rows(db_conn, 9999) == []


def test_get_agents_returns_spawned(db_conn: psycopg.Connection) -> None:
    """GET /api/agents returns agents from agents_meta table."""
    t1 = _seed_agent(db_conn)
    t2 = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.get("/api/agents")
    assert resp.status_code == 200
    rows = resp.json()
    ids = {r["agent_id"] for r in rows}
    assert t1 in ids
    assert t2 in ids
    assert all(r["label"] is None for r in rows if r["agent_id"] in (t1, t2))
