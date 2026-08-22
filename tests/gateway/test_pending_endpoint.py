"""GET /api/agents/{id}/pending integration tests.

FastAPI TestClient + real ava_test DB. The endpoint returns only the
queued chat inbounds (status='pending', kind='chat'), oldest first —
claimed/done rows and control kinds are excluded, because once claimed a
message shows in the timeline snapshot instead.
"""

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import (
    insert_compact_request_inbound,
    insert_inbound_message,
    list_pending_inbounds,
)


def _seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
            (new_id,),
        )
    db_conn.commit()
    return new_id


def _set_status(db_conn: psycopg.Connection, inbound_id: int, status: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute("UPDATE inbound_messages SET status = %s WHERE id = %s", (status, inbound_id))
    db_conn.commit()


def test_pending_returns_only_pending_chat_oldest_first(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    first = insert_inbound_message(db_conn, tid, "first", source="user")
    second = insert_inbound_message(db_conn, tid, "second", source="agent:3")
    # claimed + done chat are excluded (they're in the timeline now)
    claimed = insert_inbound_message(db_conn, tid, "being processed", source="user")
    _set_status(db_conn, claimed, "claimed")
    done = insert_inbound_message(db_conn, tid, "already handled", source="user")
    _set_status(db_conn, done, "done")
    # non-chat control kind (compact_request) is excluded even while pending
    insert_compact_request_inbound(db_conn, tid)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    items = resp.json()
    assert [it["content"] for it in items] == ["first", "second"]
    assert [it["source"] for it in items] == ["user", "agent:3"]
    assert [it["id"] for it in items] == [first, second]


def test_pending_empty_when_nothing_queued(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_pending_nonexistent_agent_returns_empty(db_conn: psycopg.Connection) -> None:
    # Lenient read: no 404 precondition (same as the timeline GET).
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_pending_inbounds_helper_scopes_by_agent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    insert_inbound_message(db_conn, a, "for a", source="user")
    insert_inbound_message(db_conn, b, "for b", source="user")
    rows = list_pending_inbounds(db_conn, a)
    assert [r.content for r in rows] == ["for a"]
