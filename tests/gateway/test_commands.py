"""GET /api/commands — serves command metadata (no body) for the autocomplete."""

from collections.abc import Callable
from typing import NoReturn

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from ava import _commands as ava_commands
from gateway.app import app
from gateway.routers import commands as commands_router


def _runner_a(_request: Request, _agent_id: int) -> str:
    return "runner-a"


def _missing_agent(_request: Request, _agent_id: int) -> None:
    return None


def _runner_unreachable() -> commands_router._cluster_rpc.ClusterOpUnreachable:
    return commands_router._cluster_rpc.ClusterOpUnreachable("runner down")


def _runner_failed() -> commands_router._cluster_rpc.ClusterOpFailed:
    return commands_router._cluster_rpc.ClusterOpFailed({"error": "unknown op"})


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

    monkeypatch.setattr(commands_router, "_agent_machine", _runner_a)

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, int], *, timeout_s: float
    ) -> dict[str, object]:
        seen["machine"] = machine
        seen["kind"] = kind
        seen["payload"] = payload
        seen["timeout_s"] = timeout_s
        return {
            "commands": [{"name": "project", "description": "d", "instruction_hint": "h"}],
            "mcp_names": ["runner-only-groundwork"],
        }

    monkeypatch.setattr(commands_router._cluster_rpc, "dispatch_to_machine", _dispatch)
    with TestClient(app) as client:
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
    monkeypatch.setattr(commands_router, "_agent_machine", _runner_a)
    monkeypatch.setattr(
        ava_commands,
        "discover_commands",
        lambda: [{"name": "local", "description": "d", "instruction_hint": "h", "body": "b"}],
    )

    async def _unavailable(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure()

    monkeypatch.setattr(commands_router._cluster_rpc, "dispatch_to_machine", _unavailable)
    with TestClient(app) as client:
        resp = client.get("/api/commands?agent_id=42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "local", "description": "d", "instruction_hint": "h"}]


def test_endpoint_agent_view_missing_agent_falls_back_locally(monkeypatch: pytest.MonkeyPatch):
    """A missing agents_meta row takes the same backward-compatible fallback."""
    monkeypatch.setattr(commands_router, "_agent_machine", _missing_agent)
    monkeypatch.setattr(
        ava_commands,
        "discover_commands",
        lambda: [{"name": "local", "description": "d", "instruction_hint": "h", "body": "b"}],
    )
    with TestClient(app) as client:
        resp = client.get("/api/commands?agent_id=999")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "local", "description": "d", "instruction_hint": "h"}]
