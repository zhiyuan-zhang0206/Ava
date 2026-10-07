"""Schedule writes preserve no-op identity and transactionally enqueue work."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

import base.db
from gateway.app import app
from gateway.schedules import router, session_control


def _create(client: TestClient) -> int:
    response = client.post("/api/schedules", json={"name": "delivery", "script": "print(1)\n"})
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.parametrize(
    "fields",
    [
        {"script": "print(1)\n"},
        {"command": "python schedule.py"},
        {"script": "print(1)\n", "enabled": True},
    ],
)
def test_same_value_edit_preserves_version_timestamp_and_queue(
    db_conn: psycopg.Connection, fields: dict[str, object]
) -> None:
    with TestClient(app) as client:
        sid = _create(client)
        before = db_conn.execute(
            "SELECT updated_at FROM schedules WHERE id = %s", (sid,)
        ).fetchone()
        response = client.put(f"/api/schedules/{sid}", json=fields)
    assert response.status_code == 200
    assert (
        db_conn.execute("SELECT updated_at FROM schedules WHERE id = %s", (sid,)).fetchone()
        == before
    )
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s", (sid,)
    ).fetchall() == [("initial",)]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []


def test_concurrent_identical_edits_append_one_version(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        sid = _create(client)
    with base.db.pool(max_size=4) as pool, ThreadPoolExecutor(max_workers=4) as workers:

        def edit(_worker: int) -> tuple[tuple[object, ...], bool]:
            return router._update_blocking(pool, sid, {"script": "print(2)\n"})

        results = list(workers.map(edit, range(4)))
    assert sum(needs_sync for _, needs_sync in results) == 1
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s ORDER BY id", (sid,)
    ).fetchall() == [("initial",), ("edit",)]
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]


def test_enqueue_failure_rolls_back_config_and_version(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(app) as client:
        sid = _create(client)

    def fail(conn: object, schedule_id: int) -> None:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(session_control, "enqueue_in_transaction", fail)
    with base.db.pool() as pool, pytest.raises(RuntimeError, match="queue unavailable"):
        router._update_blocking(pool, sid, {"script": "print(2)\n"})
    assert db_conn.execute("SELECT script FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "print(1)\n",
    )
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s", (sid,)
    ).fetchall() == [("initial",)]


def test_response_failure_leaves_committed_convergence_work(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(pool: object, schedule_id: int) -> None:
        raise RuntimeError("response lost")

    monkeypatch.setattr(session_control, "wait_consumed", fail)
    with TestClient(app) as client:
        sid = _create(client)
        with pytest.raises(RuntimeError, match="response lost"):
            client.put(f"/api/schedules/{sid}", json={"script": "print(2)\n"})
        # The retry is a no-op; it must preserve the pending original delivery.
        assert client.put(f"/api/schedules/{sid}", json={"script": "print(2)\n"}).status_code == 200
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]
    assert db_conn.execute(
        "SELECT note FROM schedule_versions WHERE schedule_id = %s ORDER BY id", (sid,)
    ).fetchall() == [("initial",), ("edit",)]


@pytest.mark.parametrize("enabled,action", [(True, "start"), (False, "stop")])
def test_repeated_enabled_choice_does_not_enqueue(
    db_conn: psycopg.Connection, enabled: bool, action: str
) -> None:
    with TestClient(app) as client:
        sid = client.post(
            "/api/schedules", json={"name": "choice", "script": "pass", "enabled": enabled}
        ).json()["id"]
        assert client.post(f"/api/schedules/{sid}/{action}").status_code == 200
        assert client.post(f"/api/schedules/{sid}/{action}").status_code == 200
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []


@pytest.mark.parametrize("status", ["completed", "error"])
def test_explicit_start_preserves_terminal_rerun(db_conn: psycopg.Connection, status: str) -> None:
    with TestClient(app) as client:
        sid = _create(client)
        db_conn.execute("UPDATE schedules SET status = %s WHERE id = %s", (status, sid))
        db_conn.commit()
        assert client.post(f"/api/schedules/{sid}/start").status_code == 200
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]
