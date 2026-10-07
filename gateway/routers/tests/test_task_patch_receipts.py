"""Task PATCH retries preserve the first commit across later task changes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.agents.tasks.priority import Priority
from gateway.app import app
from gateway.routers import tasks
from gateway.routers.tests.test_tasks_router import _make_agent, _make_task
from gateway.schemas.tasks import TaskUpdateRequest


def _count(db: psycopg.Connection, table: str) -> int:
    from psycopg import sql

    row = db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()
    assert row is not None
    return int(row[0])


def test_concurrent_duplicates_commit_one_assignment(db_conn: psycopg.Connection) -> None:
    old, new = _make_agent(db_conn), _make_agent(db_conn)
    tid = _make_task(db_conn, owner=old)
    barrier = Barrier(2)
    with TestClient(app):

        def patch(_index: int) -> tuple[object, int]:
            barrier.wait()
            task, notes = tasks._patch_task_blocking(
                app.state.db_pool, tid, TaskUpdateRequest(owner=new), "same-operation"
            )
            return task, len(notes)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(patch, range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [0, 2]
    assert _count(db_conn, "task_patch_receipts") == 1
    assert _count(db_conn, "inbound_messages") == 2


def test_lost_response_replay_preserves_new_owner_and_reminder_window(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, middle, current = (_make_agent(db_conn) for _ in range(3))
    tid = _make_task(db_conn, owner=old)
    headers = {"Idempotency-Key": "lost-response"}
    real_wake = tasks.publish_inbound_wake

    def fail_wake(*_args: object) -> None:
        raise RuntimeError("post-commit response lost")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(tasks, "publish_inbound_wake", fail_wake)
        assert (
            client.patch(f"/api/tasks/{tid}", json={"owner": middle}, headers=headers).status_code
            == 500
        )
        recorded = db_conn.execute("SELECT result FROM task_patch_receipts").fetchone()
        assert recorded is not None
        original = recorded[0]
        monkeypatch.setattr(tasks, "publish_inbound_wake", real_wake)
        assert client.patch(f"/api/tasks/{tid}", json={"owner": current}).status_code == 200
        db_conn.execute(
            "UPDATE agent_tasks SET reminder_count=4, last_reminded_at=now(), escalated_at=now() WHERE id=%s",
            (tid,),
        )
        db_conn.commit()
        before = db_conn.execute(
            "SELECT owner, updated_at, reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id=%s",
            (tid,),
        ).fetchone()
        inbounds = db_conn.execute(
            "SELECT id, status, payload FROM inbound_messages ORDER BY id"
        ).fetchall()
        monkeypatch.setattr(tasks, "publish_inbound_wake", fail_wake)
        monkeypatch.setattr(tasks._ops, "resurrect_if_terminated", fail_wake)
        replay = client.patch(f"/api/tasks/{tid}", json={"owner": middle}, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == original
    assert (
        db_conn.execute(
            "SELECT owner, updated_at, reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id=%s",
            (tid,),
        ).fetchone()
        == before
    )
    assert (
        db_conn.execute("SELECT id, status, payload FROM inbound_messages ORDER BY id").fetchall()
        == inbounds
    )


def test_snapshot_replays_after_task_deletion(db_conn: psycopg.Connection) -> None:
    tid = _make_task(db_conn, owner=_make_agent(db_conn))
    headers = {"Idempotency-Key": "snapshot"}
    with TestClient(app) as client:
        first = client.patch(f"/api/tasks/{tid}", json={"status": "done"}, headers=headers)
        db_conn.execute("DELETE FROM agent_tasks WHERE id=%s", (tid,))
        db_conn.commit()
        replay = client.patch(f"/api/tasks/{tid}", json={"status": "done"}, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()


def test_changed_body_conflict_and_omitted_null_distinction(db_conn: psycopg.Connection) -> None:
    tid = _make_task(db_conn, owner=_make_agent(db_conn))
    headers = {"Idempotency-Key": "body"}
    with TestClient(app) as client:
        assert (
            client.patch(f"/api/tasks/{tid}", json={"priority": "P1"}, headers=headers).status_code
            == 200
        )
        for body in ({"priority": "P2"}, {"priority": "P1", "parent_id": None}):
            assert client.patch(f"/api/tasks/{tid}", json=body, headers=headers).status_code == 409
    assert _count(db_conn, "task_patch_receipts") == 1


@pytest.mark.parametrize("failure", ["notification", "receipt"])
def test_producer_failure_rolls_back_effect_and_receipt(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    old, new = _make_agent(db_conn), _make_agent(db_conn)
    tid = _make_task(db_conn, owner=old)
    target = "enqueue_task_notifications" if failure == "notification" else "save_task_receipt"
    real = getattr(tasks, target)

    def fail_after_insert(*args: object) -> None:
        real(*args)
        raise RuntimeError("producer failed after durable insert")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(tasks, target, fail_after_insert)
        assert (
            client.patch(
                f"/api/tasks/{tid}", json={"owner": new}, headers={"Idempotency-Key": "rollback"}
            ).status_code
            == 500
        )
    assert db_conn.execute("SELECT owner FROM agent_tasks WHERE id=%s", (tid,)).fetchone() == (old,)
    assert _count(db_conn, "inbound_messages") == _count(db_conn, "task_patch_receipts") == 0


@pytest.mark.parametrize(
    "headers", [{"Idempotency-Key": ""}, {"Idempotency-Scope": "principal-v1"}]
)
def test_invalid_identity_has_no_effect(
    db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    tid = _make_task(db_conn, owner=_make_agent(db_conn))
    with TestClient(app) as client:
        assert (
            client.patch(f"/api/tasks/{tid}", json={"priority": "P1"}, headers=headers).status_code
            == 422
        )
    assert _count(db_conn, "task_patch_receipts") == 0


def test_distinct_task_paths_and_principals_are_independent(db_conn: psycopg.Connection) -> None:
    from gateway.auth.request_principal import AuthPrincipal, principal_key

    owner = _make_agent(db_conn)
    first, second = (
        _make_task(db_conn, owner=owner),
        _make_task(db_conn, owner=owner, title="second"),
    )
    with TestClient(app) as client:
        for tid in (first, second):
            assert (
                client.patch(
                    f"/api/tasks/{tid}", json={"priority": "P1"}, headers={"Idempotency-Key": "raw"}
                ).status_code
                == 200
            )
        for subject in ("one", "two"):
            key = principal_key(
                AuthPrincipal("mcp_client", subject), "PATCH", f"/api/tasks/{first}", "raw"
            )
            tasks._patch_task_blocking(
                app.state.db_pool, first, TaskUpdateRequest(priority=Priority.P2), key
            )
    assert _count(db_conn, "task_patch_receipts") == 4
