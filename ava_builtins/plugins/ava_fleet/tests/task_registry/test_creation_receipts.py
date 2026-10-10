"""Standalone SDK creation preserves its original accepted result."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from ava_builtins.plugins.ava_fleet import _task_creation_receipts, _task_update, task_registry
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import _seed_agent
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import root_task_id as root_task_id
from base import telemetry
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from tests.fixtures.pin_agent import pin_agent


def _facts(db: psycopg.Connection) -> tuple[object, ...]:
    return tuple(
        db.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall()
        for table in ("agent_tasks", "audit_events", "inbound_messages", "task_creation_receipts")
    )


def test_concurrent_creation_returns_same_original_task(
    db_conn: psycopg.Connection, root_task_id: int, *, database_gate: ProcessDbGate
) -> None:
    actor, owner = _seed_agent(db_conn), _seed_agent(db_conn)
    barrier = Barrier(2)
    before = db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone()

    def create(_index: int) -> task_registry.Task:
        clients = ClientSet(database=lambda: Database.from_settings(gate=database_gate))
        try:
            context = AvaContext(identity=AgentIdentity(actor, True), clients=clients)
            barrier.wait()
            return task_registry._create(
                context,
                "concurrent creation",
                "work",
                parent=root_task_id,
                owner=owner,
                operation_key="same",
            )
        finally:
            clients.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(create, range(2)))
    assert first == second
    assert db_conn.execute("SELECT count(*) FROM task_creation_receipts").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='task_create'"
    ).fetchone() == (1,)
    assert before is not None
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (before[0] + 1,)


def test_lost_response_returns_original_after_mutation_and_title_reuse(
    db_conn: psycopg.Connection, root_task_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_agent(_seed_agent(db_conn))
    real_emit = telemetry.emit_prepared

    def fail(*_args: object) -> None:
        raise RuntimeError("committed response lost")

    monkeypatch.setattr(telemetry, "emit_prepared", fail)
    with pytest.raises(RuntimeError, match="response lost"):
        task_registry.create("original", "work", parent=root_task_id, operation_key="lost")
    snapshot = db_conn.execute("SELECT result FROM task_creation_receipts").fetchone()
    assert snapshot is not None
    original = task_registry.Task(**snapshot[0])
    monkeypatch.setattr(telemetry, "emit_prepared", real_emit)
    task_registry.update(original.id, title="renamed", status="done", operation_key=str(uuid4()))
    task_registry.create(
        "original", "another work", parent=root_task_id, operation_key=str(uuid4())
    )
    before = _facts(db_conn)
    monkeypatch.setattr(telemetry, "emit_prepared", fail)
    assert (
        task_registry.create("original", "work", parent=root_task_id, operation_key="lost")
        == original
    )
    assert _facts(db_conn) == before


def test_deleted_task_and_parent_replay_snapshot(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    pin_agent(_seed_agent(db_conn))
    parent = task_registry.create("parent", "work", parent=root_task_id, operation_key=str(uuid4()))
    original = task_registry.create("child", "work", parent=parent.id, operation_key="deleted")
    db_conn.execute("DELETE FROM agent_tasks WHERE id IN (%s,%s)", (original.id, parent.id))
    db_conn.commit()
    before = _facts(db_conn)
    assert (
        task_registry.create("child", "work", parent=parent.id, operation_key="deleted") == original
    )
    assert _facts(db_conn) == before


def test_default_intention_replays_original_and_owner_alias(
    db_conn: psycopg.Connection, root_task_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _seed_agent(db_conn)
    pin_agent(actor)
    original = task_registry.create(
        "default", "work", parent=root_task_id, operation_key="defaults"
    )
    monkeypatch.setitem(_task_update.DEFAULT_REMIND_INTERVAL_SECONDS, _task_update.Priority.P2, 777)

    def fail_render(*_args: object) -> tuple[object, ...]:
        raise RuntimeError("replay must not render timestamps again")

    monkeypatch.setattr("base.agents.tasks.model.render_task_timestamps", fail_render)
    assert (
        task_registry.create(
            "default", "work", parent=root_task_id, owner=actor, operation_key="defaults"
        )
        == original
    )
    with pytest.raises(ValueError, match="different task creation"):
        task_registry.create(
            "default",
            "work",
            parent=root_task_id,
            remind_interval_seconds=original.remind_interval_seconds,
            operation_key="defaults",
        )


@pytest.mark.parametrize(
    "change", [{"description": "changed"}, {"parent": -1}, {"owner": -1}, {"priority": "P1"}]
)
def test_changed_request_conflicts_before_mutable_checks(
    db_conn: psycopg.Connection, root_task_id: int, change: dict[str, Any]
) -> None:
    pin_agent(_seed_agent(db_conn))
    kwargs: dict[str, Any] = {
        "title": "body",
        "description": "work",
        "parent": root_task_id,
        "operation_key": "body",
    }
    task_registry.create(**kwargs)
    before = _facts(db_conn)
    with pytest.raises(ValueError, match="different task creation"):
        task_registry.create(**(kwargs | change))
    assert _facts(db_conn) == before


@pytest.mark.parametrize("failure", ["notification", "receipt", "audit"])
def test_failure_after_effect_rolls_back_all_facts(
    db_conn: psycopg.Connection, root_task_id: int, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from base.agents.tasks import creation
    from base.telemetry import audit_events

    actor, owner = _seed_agent(db_conn), _seed_agent(db_conn)
    pin_agent(actor)
    before = _facts(db_conn)
    module, name = {
        "notification": (creation, "queue_creation_notifications"),
        "receipt": (_task_creation_receipts, "record_creation"),
        "audit": (audit_events, "record_audit"),
    }[failure]
    real = getattr(module, name)

    def fail(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)
        raise RuntimeError("producer failed")

    monkeypatch.setattr(module, name, fail)
    with pytest.raises(RuntimeError, match="producer failed"):
        task_registry.create(
            "rollback", "work", parent=root_task_id, owner=owner, operation_key="rollback"
        )
    assert _facts(db_conn) == before


@pytest.mark.parametrize("key", ["", "x" * 129, 12])
def test_invalid_key_has_no_effect(
    db_conn: psycopg.Connection, root_task_id: int, key: object
) -> None:
    pin_agent(_seed_agent(db_conn))
    before = _facts(db_conn)
    with pytest.raises((ValueError, TypeError)):
        task_registry.create("invalid", "work", parent=root_task_id, operation_key=key)  # type: ignore[arg-type]
    assert _facts(db_conn) == before


def test_snapshot_validation_never_defaults_or_accepts_unknown_enum(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    pin_agent(_seed_agent(db_conn))
    snapshot: dict[str, object] = asdict(
        task_registry.create("snapshot", "work", parent=root_task_id, operation_key=str(uuid4()))
    )
    for invalid in (
        {k: v for k, v in snapshot.items() if k != "priority"},
        snapshot | {"unknown": 1},
        snapshot | {"status": "unknown"},
        snapshot | {"priority": "unknown"},
        snapshot | {"id": "1"},
    ):
        with pytest.raises(ValueError):
            _task_creation_receipts.validate_snapshot(invalid)


def test_actor_scope_independent_and_new_key_keeps_title_constraint(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    first_actor, second_actor = _seed_agent(db_conn), _seed_agent(db_conn)
    pin_agent(first_actor)
    original = task_registry.create("first", "work", parent=root_task_id, operation_key="same")
    with pytest.raises(ValueError, match="duplicate in_progress"):
        task_registry.create("first", "work", parent=root_task_id, operation_key="new")
    pin_agent(second_actor)
    other = task_registry.create("second", "work", parent=root_task_id, operation_key="same")
    assert original.id != other.id
    assert db_conn.execute("SELECT count(*) FROM task_creation_receipts").fetchone() == (2,)
