"""GET /api/agents/{id}/activity integration tests.

FastAPI TestClient + real ava_test DB. The endpoint returns the agent's
self-reported activity trail (one row per ava.self.log() call),
oldest first — the fleet view replays it to show how the work progressed.
"""

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (new_id,),
        )
    db_conn.commit()
    return new_id


def _append(db_conn: psycopg.Connection, agent_id: int, text: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_activity (agent_id, text) VALUES (%s, %s)",
            (agent_id, text),
        )
    db_conn.commit()


def test_activity_returns_trail_oldest_first(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _append(db_conn, a, "exploring the auth module")
    _append(db_conn, a, "forked 3 workers")
    _append(db_conn, a, "merging results")

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{a}/activity")

    assert resp.status_code == 200
    items = resp.json()
    assert [it["text"] for it in items] == [
        "exploring the auth module",
        "forked 3 workers",
        "merging results",
    ]
    assert all("created_at" in it for it in items)


def test_activity_scoped_by_agent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _append(db_conn, a, "for a")
    _append(db_conn, b, "for b")

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{a}/activity")
    assert [it["text"] for it in resp.json()] == ["for a"]


def test_activity_empty_for_silent_or_nonexistent_agent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert client.get(f"/api/agents/{a}/activity").json() == []
        # Lenient read: no 404 precondition (same as the other GETs).
        assert client.get("/api/agents/999999/activity").json() == []
