"""Task effects and queued notification intents survive producer/tail crashes."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import _seed_agent
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import root_task_id as root_task_id
from base import telemetry
from base.db import fetch_one
from base.db import pool as db_pool
from gateway.app import app
from gateway.routers import tasks
from services.wake.delivery_watchdog.resurrect_retry import select_terminated_owners_with_pending
from tests.fixtures.pin_agent import pin_agent


def test_sdk_commit_survives_producer_loss(
    db_conn: psycopg.Connection,
    root_task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _seed_agent(db_conn)
    owner = _seed_agent(db_conn, status="terminated")
    pin_agent(actor)

    def lost_after_commit(*args: object) -> None:
        raise RuntimeError("producer lost after transaction")

    monkeypatch.setattr(telemetry, "emit_prepared", lost_after_commit)
    with pytest.raises(RuntimeError):
        task_registry.create(
            "survives", "work", parent=root_task_id, owner=owner, operation_key=str(uuid4())
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM agent_tasks WHERE title='survives'")
        task_id = int(fetch_one(cur, "accepted task")[0])
        cur.execute(
            "SELECT id, payload FROM inbound_messages WHERE agent_id=%s AND kind='system_note'",
            (owner,),
        )
        notes = cur.fetchall()
    assert len(notes) == 1
    assert notes[0][1]["task_id"] == task_id
    assert notes[0][1]["delivery_resurrect"] is True
    note_pool = db_pool()
    assert note_pool is not None
    assert select_terminated_owners_with_pending(note_pool, 86400) == [(owner, notes[0][0])]


def test_gateway_enqueue_failure_rolls_back_assignment(
    db_conn: psycopg.Connection,
    root_task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _seed_agent(db_conn)
    old_owner = _seed_agent(db_conn)
    new_owner = _seed_agent(db_conn)
    pin_agent(actor)
    task = task_registry.create(
        "atomic", "work", parent=root_task_id, owner=old_owner, operation_key=str(uuid4())
    )
    real = tasks.enqueue_task_notifications

    def unavailable(*args: object) -> None:
        raise psycopg.OperationalError("queue unavailable")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(tasks, "enqueue_task_notifications", unavailable)
        assert (
            client.patch(
                f"/api/tasks/{task.id}",
                json={"owner": new_owner},
                headers={"Idempotency-Key": str(uuid4())},
            ).status_code
            == 500
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT owner FROM agent_tasks WHERE id=%s", (task.id,))
            assert cur.fetchone() == (old_owner,)
            cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (new_owner,))
            assert cur.fetchone() == (0,)
        monkeypatch.setattr(tasks, "enqueue_task_notifications", real)
        assert (
            client.patch(
                f"/api/tasks/{task.id}",
                json={"owner": new_owner},
                headers={"Idempotency-Key": str(uuid4())},
            ).status_code
            == 200
        )


def test_aba_assignment_supersedes_old_directions(
    db_conn: psycopg.Connection,
    root_task_id: int,
) -> None:
    actor = _seed_agent(db_conn)
    owner_a = _seed_agent(db_conn, status="terminated")
    owner_b = _seed_agent(db_conn, status="terminated")
    pin_agent(actor)
    task = task_registry.create(
        "aba", "work", parent=root_task_id, owner=owner_a, operation_key=str(uuid4())
    )
    task_registry.update(task.id, owner=owner_b, operation_key=str(uuid4()))
    task_registry.update(task.id, owner=owner_a, operation_key=str(uuid4()))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, agent_id, status, payload FROM inbound_messages "
            "WHERE payload->'delivery_resurrect'='true'::jsonb ORDER BY id",
        )
        notes = cur.fetchall()
    assert len(notes) == 3
    assert [note[2] for note in notes] == ["done", "done", "pending"]
    assert all(note[3]["delivery_result"]["outcome"] == "superseded" for note in notes[:2])
    note_pool = db_pool()
    assert note_pool is not None
    assert select_terminated_owners_with_pending(note_pool, 86400) == [(owner_a, notes[-1][0])]
