"""Receipt rollback and lifecycle boundaries remain transactional."""

import psycopg
import pytest

import base.db
from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db.code_version_gate import ProcessDbGate
from gateway.app import app
from gateway.schedules import receipts, router, session_control
from tests.fixtures.gateway_config import gateway_test_client


def test_failed_receipt_commit_rolls_back_restart_revision_and_queue(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    with gateway_test_client(app, machine=machine_identity) as client:
        sid = client.post("/api/schedules", json={"name": "rollback", "script": "pass"}).json()[
            "id"
        ]

    def fail(*args: object) -> None:
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(receipts, "finish", fail)
    with (
        base.db.pool(gate=database_gate) as pool,
        pytest.raises(RuntimeError, match="receipt write failed"),
    ):
        router.control_blocking(pool, sid, "restart", "restart")
    assert db_conn.execute(
        "SELECT desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_sync_requests").fetchone() == (0,)


def test_receipt_replays_after_schedule_deletion(
    gateway_data_plane: None, db_conn: psycopg.Connection, *, machine_identity: MachineIdentity
) -> None:
    with gateway_test_client(app, machine=machine_identity) as client:
        sid = client.post("/api/schedules", json={"name": "deleted", "script": "pass"}).json()["id"]
        path = f"/api/schedules/{sid}/restart"
        headers = keyed_headers("before-delete")
        original = client.post(path, headers=headers)
        assert client.delete(f"/api/schedules/{sid}").status_code == 200
        assert client.post(path, headers=headers).json() == original.json()
        assert client.post(path, headers=keyed_headers("after-delete")).status_code == 404
    assert db_conn.execute("SELECT id FROM schedules WHERE id = %s", (sid,)).fetchone() is None


def test_delete_cleanup_enqueue_failure_preserves_schedule(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    with gateway_test_client(app, machine=machine_identity) as client:
        sid = client.post(
            "/api/schedules", json={"name": "delete-rollback", "script": "pass"}
        ).json()["id"]

    def fail(*args: object) -> None:
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(session_control, "enqueue_in_transaction", fail)
    with (
        base.db.pool(gate=database_gate) as pool,
        pytest.raises(RuntimeError, match="cleanup unavailable"),
    ):
        router.delete_blocking(pool, sid)
    assert db_conn.execute("SELECT id FROM schedules WHERE id = %s", (sid,)).fetchone() == (sid,)
