"""Cross-machine resurrect forward (`gateway/routers/agents_lifecycle.py:post_agent_resurrect`)
unit tests —

resurrect must forward to the home machine (a session spawn starting a new process
is a physical local-machine operation). Forward has no local shortcut: even when
the target is the local machine, the ops server executes in-process on localhost.
This endpoint covers the scenario auto-resurrect can't — a pure wake with no message
to deliver (frontend resurrect button).
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import agents_forward as forward_module
from gateway.routers import agents_lifecycle as lifecycle_module
from shared.agents import CrossMachineGatewayUnavailable, MachineNotRegistered


@pytest.fixture
def _force_local_machine(set_machine_identity) -> str:
    """Sets this unit's identity at the source via set_machine_identity so every
    machine_name() / machine_role() call site sees role=agent-runner, name='local-test'."""
    set_machine_identity(role="agent-runner", name="local-test")
    return "local-test"


def _set_agent_machine(db_conn: psycopg.Connection, agent_id: int, machine: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = 'terminated' WHERE id = %s",
            (machine, agent_id),
        )
    db_conn.commit()


class TestResurrectRouting:
    def test_remote_machine_forwards(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """machine != local → forward entire body to home machine's ops server,
        local does not touch DB / does not spawn a session."""
        captured: dict[str, Any] = {}

        async def _capture_forward(agent_id: int, path: str, json_body: dict) -> dict:
            captured["agent_id"] = agent_id
            captured["path"] = path
            captured["json_body"] = json_body
            return {"status": "spawned"}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(
                f"/api/agents/{agent_id}/resurrect",
                json={"resurrected_by": "user", "prompt": "hello"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "spawned"}
        assert captured["agent_id"] == agent_id
        assert captured["path"] == f"/api/agents/{agent_id}/resurrect-explicit-v2"
        assert captured["json_body"]["resurrected_by"] == "user"
        assert captured["json_body"]["prompt"] == "hello"

    def test_bare_resurrect_forwards_with_defaults(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bare lifecycle wake with no body (frontend resurrect button) → forward,
        prompt=None, resurrected_by defaults to 'user'. This is the scenario
        auto-resurrect cannot cover."""
        captured: dict[str, Any] = {}

        async def _capture_forward(agent_id: int, path: str, json_body: dict) -> dict:
            captured["json_body"] = json_body
            return {"status": "spawned"}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 200
        assert resp.json() == {"status": "spawned"}
        assert captured["json_body"]["prompt"] is None
        assert captured["json_body"]["resurrected_by"] == "user"

    def test_machine_not_registered_propagates_404(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """target not in machines table → lookup raises MachineNotRegistered → handler converts to 404."""

        async def _forward_raises(*args: Any, **kw: Any) -> None:
            raise MachineNotRegistered("machine 'remote-mac' not found in machines table")

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _forward_raises)
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 404
        assert resp.json()["reason"] == "machine_not_registered"

    def test_cross_machine_gateway_unavailable_propagates_502(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """target ops server transport error → CrossMachineGatewayUnavailable → 502."""

        async def _forward_raises(*args: Any, **kw: Any) -> None:
            raise CrossMachineGatewayUnavailable("target unreachable after 3 retries")

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _forward_raises)
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"

    def test_nonexistent_agent_404(self, _force_local_machine: str) -> None:
        """Nonexistent → helper's own SELECT hits AgentNotFound (404), does not query machines table."""
        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/resurrect")
        assert resp.status_code == 404


def test_local_home_machine_is_forwarded(
    _force_local_machine: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when agents_meta.machine is local, resurrect still goes through forward —
    no local shortcut. Single box is just the special case where the ops server
    happens to be on localhost."""
    captured: dict[str, Any] = {}

    async def _capture_enqueue(target: str, path: str, json_body: dict) -> dict:
        captured["target"] = target
        captured["path"] = path
        return {"status": "spawned"}

    with TestClient(app) as client:
        agent_id = client.post("/api/agents", json={}).json()["id"]
        _set_agent_machine(db_conn, agent_id, "local-test")
        monkeypatch.setattr(forward_module, "_enqueue_lifecycle", _capture_enqueue)  # pyright: ignore[reportUnknownArgumentType]
        resp = client.post(f"/api/agents/{agent_id}/resurrect")
    assert resp.status_code == 200
    assert resp.json() == {"status": "spawned"}
    assert captured["target"] == "local-test"
    assert captured["path"] == f"/api/agents/{agent_id}/resurrect-explicit-v2"


@pytest.mark.asyncio
async def test_lifecycle_forward_uses_a_short_idempotent_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable remote host must produce a gateway error before the
    caller's request deadline, without giving up retry-safe lifecycle dispatch."""
    forward = importlib.reload(forward_module)
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "enqueued"}

    monkeypatch.setattr(forward._cluster_rpc, "dispatch_to_machine", _dispatch)

    result = await forward._enqueue_lifecycle("offline-runner", "/restart", {})

    assert result == {"status": "enqueued"}
    assert isinstance(captured["timeout_s"], float)
    assert captured["timeout_s"] < 15.0
    assert captured["retries"] == 1


@pytest.mark.asyncio
async def test_lifecycle_forward_deadline_becomes_a_clear_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport that ignores its own per-attempt timeout is still bounded at
    the router boundary, where the user receives a named 502 instead of silence."""
    forward = importlib.reload(forward_module)
    never = asyncio.Event()

    async def _never_dispatch(**_kwargs: Any) -> dict[str, Any]:
        await never.wait()
        return {}

    monkeypatch.setattr(forward._cluster_rpc, "dispatch_to_machine", _never_dispatch)
    monkeypatch.setattr(forward, "_LIFECYCLE_DISPATCH_DEADLINE_S", 0.01, raising=False)

    with pytest.raises(CrossMachineGatewayUnavailable, match="did not answer"):
        await asyncio.wait_for(
            forward._enqueue_lifecycle("offline-runner", "/restart", {}), timeout=0.2
        )
