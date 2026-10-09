"""Durable system-note acceptance survives ambiguous responses and concurrent retries."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from base.db import Database, fetch_one, pool
from base.events.live.bus import EventBus
from gateway.agents.system_note import _system_note_blocking
from gateway.app import app


def test_system_note_replay_and_immutable_policy(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        agent = client.post("/api/agents", json={}).json()["id"]
        path = f"/api/agents/{agent}/system-note"
        headers = {"Idempotency-Key": "note-receipt"}
        body = {"content": "assigned", "resurrect": False}
        first = client.post(path, json=body, headers=headers)
        second = client.post(path, json=body, headers=headers)
        conflict = client.post(path, json={**body, "resurrect": True}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["inbound_id"] == second.json()["inbound_id"]
    assert conflict.status_code == 409
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (1,)


def test_concurrent_retries_return_one_inbound(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        agent = int(fetch_one(cur, "fixture insert")[0])
    db_conn.commit()
    with pool(max_size=4) as note_pool, ThreadPoolExecutor(max_workers=4) as executor:

        def send() -> int:
            return _system_note_blocking(
                database,
                event_bus,
                note_pool,
                agent,
                "note",
                "system",
                "task",
                None,
                client_message_id="concurrent-note",
                resurrect=False,
            )

        receipts = [future.result() for future in [executor.submit(send) for _ in range(4)]]
    assert len(set(receipts)) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (1,)


def test_replay_survives_reassignment_and_checks_content(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        agent = int(fetch_one(cur, "fixture insert")[0])
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        later_owner = int(fetch_one(cur, "fixture insert")[0])
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner) "
            "VALUES ('task', 'd', 'user', %s) RETURNING id",
            (agent,),
        )
        task = int(fetch_one(cur, "fixture insert")[0])
    db_conn.commit()
    with pool(max_size=1) as note_pool:
        args = (database, event_bus, note_pool, agent, "note", "system", "task", task)
        first = _system_note_blocking(*args, client_message_id="assignment-note")
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agent_tasks SET owner = %s WHERE id = %s", (later_owner, task))
        db_conn.commit()
        assert _system_note_blocking(*args, client_message_id="assignment-note") == first
        with pytest.raises(HTTPException) as caught:
            _system_note_blocking(
                database,
                event_bus,
                note_pool,
                agent,
                "changed",
                "system",
                "task",
                task,
                client_message_id="assignment-note",
            )
        assert caught.value.status_code == 409


def test_missing_key_cannot_insert_system_note(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        agent = client.post("/api/agents", json={}).json()["id"]
        response = client.post(f"/api/agents/{agent}/system-note", json={"content": "once"})
    assert response.status_code == 422
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
