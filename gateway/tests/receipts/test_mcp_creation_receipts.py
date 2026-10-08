"""Real credential acceptance, retained metadata and one-time secret boundaries."""

from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.config import settings
from gateway.app import app
from gateway.mcp_server import creation_receipts

PATH = "/api/keyed/v1/mcp/clients"
HEADERS = {"Idempotency-Key": "original", "Idempotency-Scope": "principal-v1"}
SECRET = "credential-receipt-test-secret"  # noqa: S105 -- isolated fixture


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, set_machine_identity: Any) -> Iterator[TestClient]:
    set_machine_identity(role="agent-runner", name="local-test")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app, headers={"Authorization": f"Bearer {SECRET}"}) as value:
        yield value


def test_response_loss_recovers_metadata_without_minting_or_revealing_token(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    first = client.post(PATH, json={"name": "original", "scope": "write"}, headers=HEADERS)
    assert first.status_code == 200, first.text
    body = first.json()
    token = body["token"]
    replay = client.post(PATH, json={"name": "original", "scope": "write"}, headers=HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**body, "token": None, "replayed": True}
    receipt = db_conn.execute("SELECT acceptance FROM mcp_credential_creation_receipts").fetchone()
    assert receipt is not None
    assert set(receipt[0]) == {"id", "name", "scope", "created_at"}
    assert token not in str(receipt)
    assert "token_hash" not in str(receipt)
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (1,)
    changed = client.post(PATH, json={"name": "changed"}, headers=HEADERS)
    assert changed.status_code == 409


def test_history_survives_revocation_deletion_and_name_reuse(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    body = {"name": "reuse"}
    original = client.post(PATH, json=body, headers=HEADERS).json()
    assert client.post(f"/api/mcp/clients/{original['id']}/revoke").status_code == 200
    assert client.post(PATH, json=body, headers=HEADERS).json()["token"] is None
    db_conn.execute("DELETE FROM mcp_clients WHERE id=%s", (original["id"],))
    db_conn.commit()
    successor = client.post(PATH, json=body, headers={**HEADERS, "Idempotency-Key": "new"}).json()
    assert successor["id"] != original["id"]
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.json() == {**original, "token": None, "replayed": True}
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (1,)


def test_concurrent_same_intent_has_one_credential_and_one_token_response(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    def send(_: int) -> dict[str, Any]:
        response = client.post(PATH, json={"name": "concurrent"}, headers=HEADERS)
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(send, range(4)))
    assert len({row["id"] for row in results}) == 1
    assert sum(row["token"] is not None for row in results) == 1
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (1,)


def test_failure_between_credential_and_receipt_rolls_back_both(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = creation_receipts.create_client_in_transaction

    def failed(conn: psycopg.Connection, name: str, scope: str) -> tuple[dict[str, Any], str]:
        original(conn, name, scope)
        raise RuntimeError("receipt fault")

    monkeypatch.setattr(creation_receipts, "create_client_in_transaction", failed)
    with pytest.raises(RuntimeError, match="receipt fault"):
        client.post(PATH, json={"name": "rollback"}, headers=HEADERS)
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM mcp_credential_creation_receipts").fetchone() == (
        0,
    )
    monkeypatch.setattr(creation_receipts, "create_client_in_transaction", original)
    assert client.post(PATH, json={"name": "rollback"}, headers=HEADERS).status_code == 200


@pytest.mark.parametrize(
    "headers", [{}, {"Idempotency-Key": "key"}, {**HEADERS, "Idempotency-Scope": "legacy"}]
)
def test_missing_or_unknown_scope_has_no_effect(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    assert client.post(PATH, json={"name": "refused"}, headers=headers).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (0,)


def test_revoked_human_credential_cannot_recover_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post(PATH, json={"name": "protected"}, headers=HEADERS).status_code == 200
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "replacement-human-secret")
    assert client.post(PATH, json={"name": "protected"}, headers=HEADERS).status_code == 401


def test_commit_succeeds_before_response_failure_replay_cannot_reissue_token(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = creation_receipts.write_transaction

    @contextmanager
    def lost_commit_response(pool: ConnectionPool) -> Generator[psycopg.Connection]:
        with transaction(pool) as conn:
            yield conn
        raise RuntimeError("committed response lost")

    monkeypatch.setattr(creation_receipts, "write_transaction", lost_commit_response)
    with pytest.raises(RuntimeError, match="committed response lost"):
        client.post(PATH, json={"name": "commit-loss"}, headers=HEADERS)
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (1,)
    monkeypatch.setattr(creation_receipts, "write_transaction", transaction)
    recovered = client.post(PATH, json={"name": "commit-loss"}, headers=HEADERS)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["token"] is None
    assert recovered.json()["replayed"] is True
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (1,)


def test_mcp_bearer_is_not_a_human_creation_or_recovery_credential(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    created = client.post(PATH, json={"name": "human-only"}, headers=HEADERS).json()
    forbidden = client.post(
        PATH,
        json={"name": "human-only"},
        headers={**HEADERS, "Authorization": f"Bearer {created['token']}"},
    )
    assert forbidden.status_code in (401, 403)
    assert created["token"] not in forbidden.text
    assert db_conn.execute("SELECT count(*) FROM mcp_clients").fetchone() == (1,)
