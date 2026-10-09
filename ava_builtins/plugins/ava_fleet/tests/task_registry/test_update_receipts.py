"""Real SDK mutations commit once per actor, task and caller operation key."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from ava_builtins.plugins.ava_fleet import _task_receipts, task_registry
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import _seed_agent
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import root_task_id as root_task_id
from base import telemetry
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.db import Database
from tests.fixtures.pin_agent import pin_agent


def _setup(db: psycopg.Connection, root: int) -> tuple[int, int, int]:
    actor, owner = _seed_agent(db), _seed_agent(db)
    pin_agent(actor)
    task = task_registry.create(
        "receipt task", "work", parent=root, owner=owner, operation_key=str(uuid4())
    )
    return actor, owner, task.id


def _facts(db: psycopg.Connection, tid: int) -> tuple[object, object, object]:
    task = db.execute(
        "SELECT owner, results, updated_at, reminder_count, last_reminded_at, escalated_at "
        "FROM agent_tasks WHERE id=%s",
        (tid,),
    ).fetchone()
    audit = db.execute("SELECT id, attributes FROM audit_events ORDER BY id").fetchall()
    notes = db.execute("SELECT id, status, payload FROM inbound_messages ORDER BY id").fetchall()
    return task, audit, notes


def test_concurrent_duplicate_note_has_one_business_effect(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor, _owner, tid = _setup(db_conn, root_task_id)
    before_notes = db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone()
    barrier = Barrier(2)

    def append(_index: int) -> None:
        clients = ClientSet(database=Database.from_settings)
        try:
            context = AvaContext(identity=AgentIdentity(actor, True), clients=clients)
            barrier.wait()
            task_registry._update(context, tid, note="one progress line", operation_key="same")
        finally:
            clients.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(append, range(2)))
    result = task_registry.get(tid).results
    assert result is not None and result.count("one progress line") == 1
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (1,)
    assert before_notes is not None
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='task_update'"
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (
        before_notes[0] + 1,
    )


def test_post_commit_failure_replay_preserves_intervening_state(
    db_conn: psycopg.Connection, root_task_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _actor, _owner, tid = _setup(db_conn, root_task_id)
    real_emit = telemetry.emit_prepared

    def fail_emit(*_args: object) -> None:
        raise RuntimeError("committed response lost")

    monkeypatch.setattr(telemetry, "emit_prepared", fail_emit)
    with pytest.raises(RuntimeError, match="response lost"):
        task_registry.log(tid, "committed", operation_key="lost")
    monkeypatch.setattr(telemetry, "emit_prepared", real_emit)
    current = _seed_agent(db_conn)
    task_registry.update(tid, owner=current, operation_key=str(uuid4()))
    db_conn.execute(
        "UPDATE agent_tasks SET reminder_count=4, escalated_at=now() WHERE id=%s", (tid,)
    )
    db_conn.commit()
    before = _facts(db_conn, tid)
    monkeypatch.setattr(telemetry, "emit_prepared", fail_emit)
    assert task_registry.update(tid, note="committed", operation_key="lost") is None
    assert _facts(db_conn, tid) == before


@pytest.mark.parametrize("failure", ["notification", "receipt", "audit"])
def test_producer_failure_rolls_back_entire_operation(
    db_conn: psycopg.Connection, root_task_id: int, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from base.telemetry import audit_events

    _actor, _owner, tid = _setup(db_conn, root_task_id)
    before = _facts(db_conn, tid)
    module, name = {
        "notification": (task_registry, "_queue_after_update"),
        "receipt": (_task_receipts, "record_update"),
        "audit": (audit_events, "record_audit"),
    }[failure]
    real = getattr(module, name)

    def fail_after_effect(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)
        raise RuntimeError("producer failed")

    monkeypatch.setattr(module, name, fail_after_effect)
    with pytest.raises(RuntimeError, match="producer failed"):
        task_registry.log(tid, "rolled back", operation_key="rollback")
    assert _facts(db_conn, tid) == before
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (0,)


def test_effective_body_aliases_and_conflicts(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, tid = _setup(db_conn, root_task_id)
    task_registry.log(tid, "", operation_key="body")
    before = _facts(db_conn, tid)
    task_registry.update(
        tid, note="", owner=None, remind_interval_seconds=None, operation_key="body"
    )
    assert _facts(db_conn, tid) == before
    for kwargs in (
        {"parent_id": None, "note": ""},
        {"note": "changed"},
        {"priority": "P1", "note": ""},
    ):
        with pytest.raises(ValueError, match="different task update"):
            task_registry.update(tid, operation_key="body", **kwargs)  # type: ignore[arg-type]
    assert _facts(db_conn, tid) == before


def test_actor_and_task_scopes_are_independent(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor, owner, tid = _setup(db_conn, root_task_id)
    second = task_registry.create("other", "work", parent=root_task_id, operation_key=str(uuid4()))
    for who, target in ((actor, tid), (actor, second.id), (owner, tid)):
        pin_agent(who)
        task_registry.log(target, "separate", operation_key="same raw key")
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (3,)


def test_retained_void_receipt_survives_task_deletion(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, tid = _setup(db_conn, root_task_id)
    task_registry.update(tid, priority="P1", operation_key="deleted")
    db_conn.execute("DELETE FROM agent_tasks WHERE id=%s", (tid,))
    db_conn.commit()
    assert task_registry.update(tid, priority="P1", operation_key="deleted") is None
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (1,)
