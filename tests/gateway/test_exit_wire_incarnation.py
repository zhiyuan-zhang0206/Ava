"""Exercise the actual SDK POST normalization against the gateway route."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from ava import _gateway_client
from gateway.app import app
from tests.agent.test_runtime_incarnation import _replace, _row


@pytest.mark.parametrize("owned", [False, True])
def test_legacy_sdk_empty_exit_body_obeys_runtime_fence(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    aid = _row(db_conn)
    if owned:
        _replace(db_conn, aid)
    with TestClient(app) as client:
        monkeypatch.setattr(_gateway_client, "_client_singleton", lambda: client)
        _gateway_client.exited(aid)
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (aid,)).fetchone() == (
        "running" if owned else "terminated",
    )


@pytest.mark.parametrize("field", ["generation", "owner"])
def test_partial_exit_incarnation_is_rejected(field: str) -> None:
    with TestClient(app) as client:
        assert client.post("/api/agents/999/exited", json={field: str(uuid4())}).status_code == 422
