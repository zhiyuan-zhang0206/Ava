"""POST /api/cancel — durable cancel inbound contract.

The endpoint INSERTs a kind='cancel' pending inbound (the durable pause signal),
returning status='enqueued'; a dead agent short-circuits to 'already_terminated'
without inserting. Replaces the old fire-and-forget Redis control-channel publish.
Runs the real FastAPI app against the test DB.
"""

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _seed_agent(db_conn: psycopg.Connection, status: str = "running") -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (new_id, status),
        )
    db_conn.commit()
    return new_id


def _pending_kinds(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def test_post_cancel_inserts_durable_cancel_inbound(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn, status="running")
    with TestClient(app) as client:
        resp = client.post("/api/cancel", json={"agent_id": tid})
    assert resp.status_code == 200
    assert resp.json() == {"status": "enqueued"}
    assert _pending_kinds(db_conn, tid) == [("cancel", "pending")]


def test_post_cancel_terminated_agent_is_noop(db_conn: psycopg.Connection) -> None:
    """A dead agent has nothing to pause — no row inserted (it would sit pending
    forever / fire a spurious pause on a future resurrect)."""
    tid = _seed_agent(db_conn, status="terminated")
    with TestClient(app) as client:
        resp = client.post("/api/cancel", json={"agent_id": tid})
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_terminated"}
    assert _pending_kinds(db_conn, tid) == []


def test_post_cancel_404_when_agent_missing(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/cancel", json={"agent_id": 9999})
    assert resp.status_code == 404
    assert _pending_kinds(db_conn, 9999) == []


def test_cancel_endpoint_without_agent_id_returns_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/cancel", json={})
    assert resp.status_code == 422


def test_cancel_endpoint_with_invalid_agent_id_returns_422() -> None:
    """agent_id=0 fails the gt=0 constraint."""
    with TestClient(app) as client:
        resp = client.post("/api/cancel", json={"agent_id": 0})
    assert resp.status_code == 422
