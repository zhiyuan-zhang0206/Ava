"""Delegator escalation marker (`escalated_at`) — the marker's whole lifecycle.

The delegation leg of the task-maintenance daemon reports an unresponsive owner
by delivering one digest to the delegator. `escalated_at`, stamped in the same
transaction as the digest's inbound insert, makes that at-most-once per overdue
window: a failed delivery leaves it unset (the next sweep retries), and both
update() paths clear it with the reminder counters (re-arming the task's next
window). The daemon's reminder/escalation behaviour suite lives in
test_task_maintenance_daemon.py; the marker tests keep the direct delivery real
because the marker write lives inside it, and the gateway leg exercises the real
HTTP boundary — kept together so the lifecycle reads in one place.
"""

from __future__ import annotations

from itertools import count
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ava_builtins.plugins.ava_fleet.task_maintenance import daemon
from ava_builtins.plugins.ava_fleet.task_maintenance.daemon import _run_escalate
from gateway.app import app
from shared.config import settings

_TASK_TITLE = count(1)


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _make_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (aid,),
        )
    db.commit()
    return aid


def _make_task(
    db: psycopg.Connection,
    *,
    owner: int,
    parent_id: int | None = None,
    remind_interval_seconds: int | None = 1800,
    updated_s_ago: float = 0.0,
    reminder_count: int = 0,
    title: str | None = None,
) -> int:
    # Distinct titles by default: the agent_tasks partial unique index forbids
    # two in_progress rows sharing a title, and these tests create many.
    if title is None:
        title = f"t-{next(_TASK_TITLE)}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by, "
            "parent_id, remind_interval_seconds) "
            "VALUES (%s, 'd', 'in_progress', %s, 'user', %s, %s) RETURNING id",
            (title, owner, parent_id, remind_interval_seconds),
        )
        tid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "UPDATE agent_tasks SET updated_at = now() - make_interval(secs => %s) WHERE id = %s",
            (updated_s_ago, tid),
        )
        if reminder_count:
            cur.execute(
                "UPDATE agent_tasks SET reminder_count = %s WHERE id = %s",
                (reminder_count, tid),
            )
    db.commit()
    return tid


def _stalled_subtask(db: psycopg.Connection, *, reminder_count: int = 3) -> tuple[int, int, int]:
    """A delegated, stalled subtask: (parent_owner, owner, task_id)."""
    parent_owner = _make_agent(db)
    parent = _make_task(db, owner=parent_owner, remind_interval_seconds=None)
    owner = _make_agent(db)
    tid = _make_task(
        db,
        owner=owner,
        parent_id=parent,
        remind_interval_seconds=1800,
        updated_s_ago=7200,
        reminder_count=reminder_count,
    )
    return parent_owner, owner, tid


def _inbound_messages(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    """Inbound rows for an agent: (content, kind, source)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def _escalated_at(db: psycopg.Connection, tid: int) -> Any:
    """The task's delegator escalation marker (None while unescalated)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute("SELECT escalated_at FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_delegator_escalation_at_most_once_per_overdue_window(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Two sweeps inside one stalled window deliver one digest: the first
    stamps `escalated_at`, so the second re-selects the task and skips it.
    (Defect class fixed here: every 5-min sweep re-sent the digest while
    reminder_count sat at the threshold — reminder delivery is backoff-gated
    for up to 24h.)"""
    parent_owner, _owner, tid = _stalled_subtask(db_conn)
    assert _run_escalate(pool, 3) == 1
    assert _run_escalate(pool, 3) == 0
    assert len(_inbound_messages(db_conn, parent_owner)) == 1
    assert _escalated_at(db_conn, tid) is not None


def test_delegator_escalation_retries_after_delivery_failure(
    pool: ConnectionPool, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed digest leaves no marker (no message landed), so the next sweep
    retries; once it lands, later sweeps stay quiet."""
    parent_owner, _owner, tid = _stalled_subtask(db_conn)
    real = daemon._deliver_message
    attempts = {"n": 0}

    def _flaky(
        pool_: ConnectionPool,
        agent_id: int,
        message: str,
        *,
        escalate_task_ids: list[int] | None = None,
    ) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg.OperationalError("db blip")
        real(pool_, agent_id, message, escalate_task_ids=escalate_task_ids)

    monkeypatch.setattr(daemon, "_deliver_message", _flaky)
    assert _run_escalate(pool, 3) == 0  # delivery failed: nothing was sent
    assert _inbound_messages(db_conn, parent_owner) == []
    assert _escalated_at(db_conn, tid) is None
    assert _run_escalate(pool, 3) == 1  # retried and delivered
    assert len(_inbound_messages(db_conn, parent_owner)) == 1
    assert _run_escalate(pool, 3) == 0  # marker holds: no third send
    assert len(_inbound_messages(db_conn, parent_owner)) == 1


def test_delegator_escalation_rearms_after_owner_update(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Any update() clears the marker with the reminder counters; after the
    owner re-engages and a later window re-crosses the threshold, the delegator
    is told again."""
    parent_owner, _owner, tid = _stalled_subtask(db_conn)
    assert _run_escalate(pool, 3) == 1
    assert _run_escalate(pool, 3) == 0
    # The owner updates the task: both update() paths reset the bookkeeping
    # (test_update_resets_reminder_count / the gateway PATCH test below cover
    # the real paths; mirror their write here).
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET last_reminded_at = NULL, reminder_count = 0, "
            "escalated_at = NULL WHERE id = %s",
            (tid,),
        )
    db_conn.commit()
    # The window starts over and its reminders re-cross the threshold.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now() WHERE id = %s",
            (tid,),
        )
    db_conn.commit()
    assert _run_escalate(pool, 3) == 1
    assert len(_inbound_messages(db_conn, parent_owner)) == 2


def test_gateway_patch_clears_escalation_marker(db_conn: psycopg.Connection) -> None:
    """The gateway update() path clears the marker with the reminder counters —
    the same reset as the SDK path (test_update_resets_reminder_count)."""
    owner = _make_agent(db_conn)
    tid = _make_task(db_conn, owner=owner)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now(), "
            "escalated_at = now() WHERE id = %s",
            (tid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.patch(f"/api/tasks/{tid}", json={"priority": "P1"})
    assert resp.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id = %s",
            (tid,),
        )
        row = cur.fetchone()
    assert row == (0, None, None)
