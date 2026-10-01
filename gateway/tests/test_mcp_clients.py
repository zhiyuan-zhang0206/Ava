"""Admin API contracts for gateway MCP client credentials."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gateway.app import app


def test_create_shows_token_once_and_list_redacts_credentials() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/mcp/clients",
            json={"name": "codex", "scope": "write"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["id"] > 0
        assert body["name"] == "codex"
        assert body["scope"] == "write"
        assert isinstance(body["token"], str)
        assert len(body["token"]) >= 32

        listed = client.get("/api/mcp/clients")
        assert listed.status_code == 200, listed.text

    rows = listed.json()
    assert len(rows) == 1
    assert set(rows[0]) == {
        "id",
        "name",
        "scope",
        "created_at",
        "revoked_at",
        "last_used_at",
    }
    assert rows[0]["id"] == body["id"]
    assert rows[0]["name"] == "codex"
    assert rows[0]["scope"] == "write"
    assert isinstance(rows[0]["created_at"], str)
    assert rows[0]["revoked_at"] is None
    assert rows[0]["last_used_at"] is None
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
