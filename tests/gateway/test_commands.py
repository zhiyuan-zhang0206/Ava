"""GET /api/commands — serves command metadata (no body) for the autocomplete."""

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import commands as commands_router


def test_endpoint_returns_command_metadata(monkeypatch: pytest.MonkeyPatch):
    # discover_commands yields full Commands (with body); the endpoint must
    # strip the body and serve only what the dropdown needs.
    monkeypatch.setattr(
        commands_router,
        "discover_commands",
        lambda: [{"name": "recap", "description": "d", "instruction_hint": "h", "body": "b"}],
    )
    with TestClient(app) as client:
        resp = client.get("/api/commands")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "recap", "description": "d", "instruction_hint": "h"}]
