"""GET /api/commands — serves command metadata (no body) for the autocomplete."""

from collections.abc import Callable
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from ava.skills import composer_commands as ava_commands
from gateway.app import app
from ops.cluster import rpc as cluster_rpc


def _agent_pool(machine: str | None) -> MagicMock:
    pool = MagicMock()
    cursor = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (machine,) if machine else None
    return pool


def _runner_unreachable() -> cluster_rpc.ClusterOpUnreachable:
    return cluster_rpc.ClusterOpUnreachable("runner down")


def _runner_failed() -> cluster_rpc.ClusterOpFailed:
    return cluster_rpc.ClusterOpFailed({"error": "unknown op"})


def test_endpoint_returns_command_metadata(monkeypatch: pytest.MonkeyPatch):
    # discover_commands yields full Commands (with body); the endpoint must
    # strip the body and serve only what the dropdown needs.
    monkeypatch.setattr(
        ava_commands,
        "discover_commands",
        lambda: [{"name": "recap", "description": "d", "instruction_hint": "h", "body": "b"}],
    )
    with TestClient(app) as client:
        resp = client.get("/api/commands")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "recap", "description": "d", "instruction_hint": "h"}]


def test_endpoint_agent_view_dispatches_to_agents_machine(monkeypatch: pytest.MonkeyPatch):
    """An agent-scoped request forwards to its runner's new view op."""
    seen: dict[str, object] = {}

    async def _dispatch(
        _db: object, machine: str, kind: str, payload: dict[str, int], *, timeout_s: float
    ) -> dict[str, object]:
        seen["machine"] = machine
        seen["kind"] = kind
        seen["payload"] = payload
        seen["timeout_s"] = timeout_s
        return {
            "commands": [{"name": "project", "description": "d", "instruction_hint": "h"}],
            "mcp_names": ["runner-only-groundwork"],
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _dispatch)
    with TestClient(app) as client, monkeypatch.context() as patch:
        patch.setattr(app.state, "db_pool", _agent_pool("runner-a"))
        resp = client.get("/api/commands?agent_id=42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "project", "description": "d", "instruction_hint": "h"}]
    assert seen == {
        "machine": "runner-a",
        "kind": "agent_skill_view",
        "payload": {"agent_id": 42},
        "timeout_s": 3.0,
    }


@pytest.mark.parametrize(
    "failure", [_runner_unreachable, _runner_failed], ids=["unreachable", "failed"]
)
def test_endpoint_agent_view_unavailable_falls_back_locally(
    monkeypatch: pytest.MonkeyPatch,
    failure: Callable[[], Exception],
):
    """A down or version-skewed runner leaves autocomplete usable from the local list."""
    monkeypatch.setattr(
        ava_commands,
        "discover_commands",
        lambda: [{"name": "local", "description": "d", "instruction_hint": "h", "body": "b"}],
    )

    async def _unavailable(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure()

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unavailable)
    with TestClient(app) as client, monkeypatch.context() as patch:
        patch.setattr(app.state, "db_pool", _agent_pool("runner-a"))
        resp = client.get("/api/commands?agent_id=42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "local", "description": "d", "instruction_hint": "h"}]


def test_endpoint_agent_view_missing_agent_falls_back_locally(monkeypatch: pytest.MonkeyPatch):
    """A missing agents_meta row takes the same backward-compatible fallback."""
    monkeypatch.setattr(
        ava_commands,
        "discover_commands",
        lambda: [{"name": "local", "description": "d", "instruction_hint": "h", "body": "b"}],
    )
    with TestClient(app) as client, monkeypatch.context() as patch:
        patch.setattr(app.state, "db_pool", _agent_pool(None))
        resp = client.get("/api/commands?agent_id=999")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "local", "description": "d", "instruction_hint": "h"}]
