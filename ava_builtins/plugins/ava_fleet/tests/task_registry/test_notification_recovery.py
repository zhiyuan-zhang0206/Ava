"""Task effects and queued notification intents survive producer/tail crashes."""

from uuid import uuid4

import psycopg
import pytest

from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.task_registry.seeds import root_task_id as root_task_id
from ava_builtins.plugins.ava_fleet.tests.task_registry.seeds import seed_agent
from base import telemetry
from base.db import fetch_one
from base.db import pool as db_pool
from base.db.code_version_gate import ProcessDbGate
from tests.fixtures.pin_agent import pin_agent


def test_sdk_commit_survives_producer_loss(
    db_conn: psycopg.Connection,
    root_task_id: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    actor = seed_agent(db_conn)
    owner = seed_agent(db_conn, status="terminated")
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
    note_pool = db_pool(gate=database_gate)
    assert note_pool is not None


def test_aba_assignment_supersedes_old_directions(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, *, database_gate: ProcessDbGate
) -> None:
    actor = seed_agent(db_conn)
    owner_a = seed_agent(db_conn, status="terminated")
    owner_b = seed_agent(db_conn, status="terminated")
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
    note_pool = db_pool(gate=database_gate)
    assert note_pool is not None
