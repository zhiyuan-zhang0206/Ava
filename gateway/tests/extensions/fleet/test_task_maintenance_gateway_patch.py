"""The gateway PATCH path clears the escalation marker.

Split out of `ava_builtins/plugins/ava_fleet/task_maintenance/tests/test_task_maintenance_escalation.py`: it drives the gateway app."""

from __future__ import annotations

from itertools import count
from uuid import uuid4

import psycopg

from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentity
from gateway.app import app
from tests.fixtures.gateway_config import gateway_test_client

_TASK_TITLE = count(1)


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


def test_gateway_patch_clears_escalation_marker(
    db_conn: psycopg.Connection, machine_identity: MachineIdentity
) -> None:
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
    with gateway_test_client(app, machine=machine_identity) as client:
        resp = client.patch(
            f"/api/tasks/{tid}", json={"priority": "P1"}, headers=keyed_headers(str(uuid4()))
        )
    assert resp.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id = %s",
            (tid,),
        )
        row = cur.fetchone()
    assert row == (0, None, None)
