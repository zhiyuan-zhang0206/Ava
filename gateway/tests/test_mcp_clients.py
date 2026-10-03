"""Admin API contracts for gateway MCP client credentials."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from gateway.app import app


def _assert_created_client(body: dict[str, Any], *, name: str, scope: str) -> None:
    assert body["id"] > 0
    assert body["name"] == name
    assert body["scope"] == scope
    assert isinstance(body["token"], str)
    assert len(body["token"]) >= 32


def _assert_listed_client_row(
    row: dict[str, Any], *, client_id: int, name: str, scope: str
) -> None:
    assert set(row) == {
        "id",
        "name",
        "scope",
        "created_at",
        "revoked_at",
        "last_used_at",
    }
    assert row["id"] == client_id
    assert row["name"] == name
    assert row["scope"] == scope
    assert isinstance(row["created_at"], str)
    assert row["revoked_at"] is None
    assert row["last_used_at"] is None


def test_create_shows_token_once_and_list_redacts_credentials() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/mcp/clients",
            json={"name": "codex", "scope": "write"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        _assert_created_client(body, name="codex", scope="write")

        listed = client.get("/api/mcp/clients")
        assert listed.status_code == 200, listed.text

    rows = listed.json()
    assert len(rows) == 1
    _assert_listed_client_row(rows[0], client_id=body["id"], name="codex", scope="write")
    assert "token" not in listed.text
    assert "hash" not in listed.text


def test_duplicate_name_returns_conflict() -> None:
    with TestClient(app) as client:
        first = client.post("/api/mcp/clients", json={"name": "claude"})
        assert first.status_code == 200, first.text

        duplicate = client.post(
            "/api/mcp/clients",
            json={"name": "claude", "scope": "write"},
        )

    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


def test_revoke_succeeds_once() -> None:
    with TestClient(app) as client:
        created = client.post("/api/mcp/clients", json={"name": "readonly"})
        assert created.status_code == 200, created.text
        client_id = created.json()["id"]

        revoked = client.post(f"/api/mcp/clients/{client_id}/revoke")
        revoked_again = client.post(f"/api/mcp/clients/{client_id}/revoke")
        missing = client.post("/api/mcp/clients/999999/revoke")

    assert revoked.status_code == 200
    assert revoked.json() == {"ok": True}
    assert revoked_again.status_code == 404
    assert missing.status_code == 404
