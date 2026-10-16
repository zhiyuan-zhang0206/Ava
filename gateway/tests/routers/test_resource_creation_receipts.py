from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest

import base.db
from base.api_contracts.idempotency import keyed_headers
from base.db.code_version_gate import ProcessDbGate
from gateway.routers.receipts import creation as creation_receipts
from gateway.schedules import router as schedules
from tests.fixtures.gateway_config import gateway_test_client

_SCHEDULE_BODY = {"name": "receipt", "script": "pass"}


def test_lost_response_retry_replays_original_created_resource(
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client() as client:
        headers = keyed_headers("one-create")
        first = client.post("/api/schedules", json=_SCHEDULE_BODY, headers=headers)
        second = client.post("/api/schedules", json=_SCHEDULE_BODY, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert db_conn.execute("SELECT count(*) FROM schedules").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM schedule_versions").fetchone() == (1,)


def test_creation_receipt_ignores_later_rename_delete_and_name_reuse(
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client() as client:
        headers = keyed_headers("original")
        first = client.post("/api/schedules", json=_SCHEDULE_BODY, headers=headers)
        sid = first.json()["id"]
        assert client.put(f"/api/schedules/{sid}", json={"name": "renamed"}).status_code == 200
        other = client.post(
            "/api/schedules", json=_SCHEDULE_BODY, headers=keyed_headers("new-intent")
        )
        assert other.status_code == 201
        assert other.json()["id"] != sid
        assert client.delete(f"/api/schedules/{sid}").status_code == 200
        replay = client.post("/api/schedules", json=_SCHEDULE_BODY, headers=headers)
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert client.get(f"/api/schedules/{sid}").status_code == 404


def test_same_creation_key_with_changed_valid_body_conflicts(
    db_conn: psycopg.Connection,
) -> None:
    body = _SCHEDULE_BODY
    with gateway_test_client() as client:
        headers = keyed_headers("immutable")
        assert client.post("/api/schedules", json=body, headers=headers).status_code == 201
        r = client.post("/api/schedules", json={**body, "name": "different"}, headers=headers)
        assert r.status_code == 409
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (1,)


def test_concurrent_creation_commits_one_resource_and_receipt(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with (
        base.db.pool(max_size=4, gate=database_gate) as pool,
        ThreadPoolExecutor(max_workers=4) as workers,
    ):

        def create(_worker: int) -> tuple[Any, ...]:
            return schedules.create_blocking(
                pool, schedules.ScheduleCreate(name="concurrent", script="pass"), "create"
            )

        results = list(workers.map(create, range(4)))
    assert all(row == results[0] for row in results)
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM schedule_versions").fetchone() == (1,)


def test_receipt_contains_no_raw_script_request(db_conn: psycopg.Connection) -> None:
    value_marker = "private-test-value"
    with gateway_test_client() as client:
        assert (
            client.post(
                "/api/schedules",
                json={"name": "opaque", "script": f"x = {value_marker!r}"},
                headers=keyed_headers("opaque-create"),
            ).status_code
            == 201
        )
    value = db_conn.execute(
        "SELECT row_to_json(r)::text FROM resource_creation_receipts r"
    ).fetchone()
    assert value is not None
    assert value_marker not in value[0]


def test_receipt_failure_rolls_back_created_resource_and_version(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
) -> None:
    def fail(*_args: object) -> None:
        raise RuntimeError("receipt failed")

    monkeypatch.setattr(creation_receipts, "finish", fail)
    with (
        base.db.pool(gate=database_gate) as pool,
        pytest.raises(RuntimeError, match="receipt failed"),
    ):
        schedules.create_blocking(
            pool, schedules.ScheduleCreate(name="rollback", script="pass"), "rollback"
        )
    assert db_conn.execute("SELECT count(*) FROM schedules").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM schedule_versions").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (0,)


def test_creation_identity_is_bound_to_verified_principal(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    from starlette.requests import Request

    from gateway.http.auth.request_principal import AuthPrincipal

    def scoped_key(subject: str) -> str | None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/schedules",
                "headers": [(b"idempotency-key", b"same"), (b"idempotency-scope", b"principal-v1")],
            }
        )
        request.state.auth_principal = AuthPrincipal("mcp_client", subject)
        return creation_receipts.operation_key(request)

    with base.db.pool(gate=database_gate) as pool:
        first = schedules.create_blocking(
            pool, schedules.ScheduleCreate(name="one", script="pass"), scoped_key("one")
        )
        second = schedules.create_blocking(
            pool, schedules.ScheduleCreate(name="two", script="pass"), scoped_key("two")
        )
    assert first[0] != second[0]
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (2,)


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": ""},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "unsupported"},
        {"Idempotency-Key": "unscoped"},
    ],
)
def test_invalid_creation_identity_cannot_reserve_resource(
    db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    with gateway_test_client() as client:
        assert (
            client.post(
                "/api/schedules", json={"name": "invalid", "script": "pass"}, headers=headers
            ).status_code
            == 400
        )
    assert db_conn.execute("SELECT count(*) FROM schedules").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM resource_creation_receipts").fetchone() == (0,)
