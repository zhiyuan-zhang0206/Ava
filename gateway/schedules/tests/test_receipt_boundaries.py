"""Receipt rollback and lifecycle boundaries remain transactional."""

import psycopg
import pytest

import base.db
from gateway.app import app
from gateway.schedules import receipts, router, session_control
from tests.fixtures.gateway_config import gateway_test_client


def test_failed_receipt_commit_rolls_back_restart_revision_and_queue(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with gateway_test_client(app) as client:
        sid = client.post("/api/schedules", json={"name": "rollback", "script": "pass"}).json()[
            "id"
        ]

    def fail(*args: object) -> None:
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(receipts, "finish", fail)
    with base.db.pool() as pool, pytest.raises(RuntimeError, match="receipt write failed"):
        router._control_blocking(pool, sid, "restart", "restart")
    assert db_conn.execute(
        "SELECT desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_sync_requests").fetchone() == (0,)


def test_receipt_replays_after_schedule_deletion(db_conn: psycopg.Connection) -> None:
    with gateway_test_client(app) as client:
        sid = client.post("/api/schedules", json={"name": "deleted", "script": "pass"}).json()["id"]
        path = f"/api/schedules/{sid}/restart"
        headers = {"Idempotency-Key": "before-delete"}
        original = client.post(path, headers=headers)
        assert client.delete(f"/api/schedules/{sid}").status_code == 200
        assert client.post(path, headers=headers).json() == original.json()
        assert client.post(path, headers={"Idempotency-Key": "after-delete"}).status_code == 404
    assert db_conn.execute("SELECT id FROM schedules WHERE id = %s", (sid,)).fetchone() is None


def test_delete_cleanup_enqueue_failure_preserves_schedule(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with gateway_test_client(app) as client:
        sid = client.post(
            "/api/schedules", json={"name": "delete-rollback", "script": "pass"}
        ).json()["id"]

    def fail(*args: object) -> None:
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(session_control, "enqueue_in_transaction", fail)
    with base.db.pool() as pool, pytest.raises(RuntimeError, match="cleanup unavailable"):
        router._delete_blocking(pool, sid)
    assert db_conn.execute("SELECT id FROM schedules WHERE id = %s", (sid,)).fetchone() == (sid,)
