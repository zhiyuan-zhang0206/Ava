"""Creation receipts survive concurrent delivery, response loss and resource changes."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

import base.db
from gateway import creation_receipts
from gateway.app import app
from gateway.routers import presets
from gateway.schedules import router as schedules


@pytest.fixture(params=["preset", "schedule"])
def resource(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any], str]:
    if request.param == "preset":
        return (
            "/api/presets",
            {
                "name": "receipt",
                "label": "worker",
                "config": {"plugin_secret": "private-test-value"},
            },
            "agent_presets",
        )
    return "/api/schedules", {"name": "receipt", "script": "pass"}, "schedules"


def test_lost_response_retry_replays_original_created_resource(
    db_conn: psycopg.Connection, resource: tuple[str, dict[str, Any], str]
) -> None:
    path, body, table = resource
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "one-create"}
        first = client.post(path, json=body, headers=headers)
        second = client.post(path, json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert db_conn.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    ).fetchone() == (1,)
    if table == "schedules":
        assert db_conn.execute("SELECT count(*) FROM schedule_versions").fetchone() == (1,)


def test_creation_receipt_ignores_later_rename_delete_and_name_reuse(
    db_conn: psycopg.Connection, resource: tuple[str, dict[str, Any], str]
) -> None:
    path, body, _table = resource
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "original"}
        first = client.post(path, json=body, headers=headers)
        sid = first.json()["id"]
        if path == "/api/presets":
            assert client.patch(f"{path}/{sid}", json={"name": "renamed"}).status_code == 200
        else:
            assert client.put(f"{path}/{sid}", json={"name": "renamed"}).status_code == 200
        other = client.post(path, json=body, headers={"Idempotency-Key": "new-intent"})
        assert other.status_code == 201
        assert other.json()["id"] != sid
        assert client.delete(f"{path}/{sid}").status_code == 200
        replay = client.post(path, json=body, headers=headers)
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert client.get(f"{path}/{sid}").status_code == 404


def test_same_creation_key_with_changed_valid_body_conflicts(
    db_conn: psycopg.Connection, resource: tuple[str, dict[str, Any], str]
) -> None:
    path, body, _table = resource
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "immutable"}
        assert client.post(path, json=body, headers=headers).status_code == 201
        assert (
            client.post(path, json={**body, "name": "different"}, headers=headers).status_code
            == 409
        )
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (1,)


@pytest.mark.parametrize("kind", ["preset", "schedule"])
def test_concurrent_creation_commits_one_resource_and_receipt(
    db_conn: psycopg.Connection, kind: str
) -> None:
    with base.db.pool(max_size=4) as pool, ThreadPoolExecutor(max_workers=4) as workers:

        def create(_worker: int) -> tuple[Any, ...]:
            if kind == "preset":
                return presets._create_blocking(
                    pool, presets.PresetCreate(name="concurrent", label="worker"), "create"
                )
            return schedules._create_blocking(
                pool, schedules.ScheduleCreate(name="concurrent", script="pass"), "create"
            )

        results = list(workers.map(create, range(4)))
    assert all(row == results[0] for row in results)
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (1,)
    if kind == "schedule":
        assert db_conn.execute("SELECT count(*) FROM schedule_versions").fetchone() == (1,)


def test_receipt_contains_no_raw_opaque_config_request(db_conn: psycopg.Connection) -> None:
    value_marker = "private-test-value"
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/presets",
                json={
                    "name": "opaque",
                    "label": "worker",
                    "config": {"plugin_secret": value_marker},
                },
                headers={"Idempotency-Key": "opaque-create"},
            ).status_code
            == 201
        )
    value = db_conn.execute(
        "SELECT row_to_json(r)::text FROM resource_creation_receipts r"
    ).fetchone()
    assert value is not None
    assert value_marker not in value[0]
    assert "plugin_secret" not in value[0]


def test_receipt_failure_rolls_back_created_resource_and_version(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> None:
        raise RuntimeError("receipt failed")

    monkeypatch.setattr(creation_receipts, "finish", fail)
    with base.db.pool() as pool, pytest.raises(RuntimeError, match="receipt failed"):
        schedules._create_blocking(
            pool, schedules.ScheduleCreate(name="rollback", script="pass"), "rollback"
        )
    assert db_conn.execute("SELECT count(*) FROM schedules").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_versions").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (0,)


def test_creation_identity_is_bound_to_verified_principal(db_conn: psycopg.Connection) -> None:
    from starlette.requests import Request

    from gateway.auth.request_principal import AuthPrincipal

    def scoped_key(subject: str) -> str | None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/presets",
                "headers": [(b"idempotency-key", b"same"), (b"idempotency-scope", b"principal-v1")],
            }
        )
        request.state.auth_principal = AuthPrincipal("mcp_client", subject)
        return creation_receipts.operation_key(request)

    with base.db.pool() as pool:
        first = presets._create_blocking(
            pool, presets.PresetCreate(name="one", label="worker"), scoped_key("one")
        )
        second = presets._create_blocking(
            pool, presets.PresetCreate(name="two", label="worker"), scoped_key("two")
        )
    assert first[0] != second[0]
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (2,)


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": ""},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "unsupported"},
    ],
)
def test_invalid_creation_identity_cannot_reserve_resource(
    db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/presets", json={"name": "invalid", "label": "worker"}, headers=headers
            ).status_code
            == 400
        )
    assert db_conn.execute("SELECT count(*) FROM agent_presets").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (0,)
