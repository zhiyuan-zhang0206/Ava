"""The fleet SDK writes record their audit fact in the transaction that makes the change.

A task create / update and a label change are agent-facing tool calls: the row and its
`audit_events` fact commit together, so a failed audit write leaves no change behind
(and the agent's retry cannot repeat a side effect that already happened).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

import ava
import ava.agent_identity
from ava_builtins.plugins.ava_fleet import plugin, task_registry
from base.telemetry import Event


def _seed_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')", (aid,)
        )
    db.commit()
    return aid


@pytest.fixture
def agent_id(db_conn: psycopg.Connection) -> Iterator[int]:
    aid = _seed_agent(db_conn)
    original = ava.agent_identity._agent_id
    ava.agent_identity._agent_id = aid
    try:
        yield aid
    finally:
        ava.agent_identity._agent_id = original


@pytest.fixture
def root_task_id(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
            "VALUES ('Root', 'root', 'in_progress', 'system', TRUE) RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        rid = int(row[0])
    db_conn.commit()
    return rid


def _audit(db: psycopg.Connection, agent_id: int, name: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT attributes FROM audit_events WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, name),
    ).fetchall()
    db.commit()
    return [row[0] for row in rows]


def _refuse(_conn: object, _event: Event) -> Event:
    raise RuntimeError("audit write failed")


def test_a_created_task_is_recorded_with_its_row(
    db_conn: psycopg.Connection, agent_id: int, root_task_id: int
) -> None:
    task = task_registry.create("audited", "detail", parent=root_task_id)

    [recorded] = _audit(db_conn, agent_id, "task_create")
    assert (recorded["task_id"], recorded["title"]) == (task.id, "audited")


def test_a_task_whose_audit_fact_cannot_be_recorded_is_not_created(
    db_conn: psycopg.Connection,
    agent_id: int,
    root_task_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("base.telemetry.audit_events.record_audit", _refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        task_registry.create("never", "detail", parent=root_task_id)

    count = db_conn.execute("SELECT count(*) FROM agent_tasks WHERE title='never'").fetchone()
    db_conn.commit()
    assert count == (0,)


def test_a_task_update_is_recorded_with_its_change(
    db_conn: psycopg.Connection, agent_id: int, root_task_id: int
) -> None:
    task = task_registry.create("title", "detail", parent=root_task_id)

    task_registry.update(task.id, remind_interval_seconds=1800)

    [recorded] = _audit(db_conn, agent_id, "task_update")
    assert recorded["task_id"] == task.id


def test_a_label_change_is_recorded_with_the_label(
    db_conn: psycopg.Connection, agent_id: int
) -> None:
    plugin.set_label("auth-refactor lead")

    assert _audit(db_conn, agent_id, "label_change") == [{"new_label": "auth-refactor lead"}]


def test_a_label_whose_audit_fact_cannot_be_recorded_is_not_set(
    db_conn: psycopg.Connection, agent_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.telemetry.audit_events.record_audit", _refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        plugin.set_label("never set")

    row = db_conn.execute("SELECT label FROM agents WHERE id=%s", (agent_id,)).fetchone()
    db_conn.commit()
    assert row == (None,)
