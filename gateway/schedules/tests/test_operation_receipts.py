"""Concurrent and delayed retries retain one schedule mutation identity."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import base.db
from gateway.app import app
from gateway.schedules import router


def _create(client: TestClient) -> int:
    return client.post("/api/schedules", json={"name": "receipt", "script": "pass"}).json()["id"]


def test_concurrent_restart_uses_one_desired_revision(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        sid = _create(client)
    with base.db.pool(max_size=4) as pool, ThreadPoolExecutor(max_workers=4) as workers:

        def restart(_worker: int) -> tuple[tuple[Any, ...], bool]:
            return router._control_blocking(pool, sid, "restart", "one-restart")

        results = list(workers.map(restart, range(4)))
    assert sum(changed for _, changed in results) == 1
    assert db_conn.execute(
        "SELECT desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM schedule_operation_receipts").fetchone() == (1,)


def test_delayed_restart_replay_cannot_undo_newer_stop(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        sid = _create(client)
        path = f"/api/schedules/{sid}/restart"
        headers = {"Idempotency-Key": "restart-one"}
        original = client.post(path, headers=headers)
        assert original.status_code == 200
        assert client.post(f"/api/schedules/{sid}/stop").status_code == 200
        assert client.post(path, headers=headers).json() == original.json()
        assert client.post(path, headers={"Idempotency-Key": "restart-two"}).status_code == 409
    assert db_conn.execute(
        "SELECT enabled, desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (False, 2)


def test_edit_receipt_replays_original_result_and_conflicts_on_changed_payload(
    db_conn: psycopg.Connection,
) -> None:
    with TestClient(app) as client:
        sid = _create(client)
        path = f"/api/schedules/{sid}"
        headers = {"Idempotency-Key": "edit-one"}
        original = client.put(path, json={"script": "print(1)"}, headers=headers)
        assert original.status_code == 200
        assert client.put(path, json={"script": "print(2)"}).status_code == 200
        assert (
            client.put(path, json={"script": "print(1)"}, headers=headers).json() == original.json()
        )
        assert client.put(path, json={"script": "print(3)"}, headers=headers).status_code == 409
    assert db_conn.execute(
        "SELECT script, desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == ("print(2)", 2)


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "x" * 129},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "unknown"},
    ],
)
def test_malformed_operation_identity_does_not_change_schedule(
    db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    with TestClient(app) as client:
        sid = _create(client)
        assert client.post(f"/api/schedules/{sid}/restart", headers=headers).status_code == 400
    assert db_conn.execute(
        "SELECT desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (0,)


def test_new_restart_identity_is_a_new_deliberate_rerun(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        sid = _create(client)
        for key in ("one", "two"):
            assert (
                client.post(
                    f"/api/schedules/{sid}/restart", headers={"Idempotency-Key": key}
                ).status_code
                == 200
            )
    assert db_conn.execute(
        "SELECT desired_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (2,)
