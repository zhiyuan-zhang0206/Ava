"""Cross-machine spawn forward (`gateway/routers/agents.py:post_agents`) unit tests —

Verify "body.machine != local → _forward_spawn_to_remote is called" routing decision + error propagation.
Actual httpx network calls are not made (mock `_forward_spawn_to_remote` intercepts); only validate router
branch + body passthrough.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import agents as app_module
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from shared.agents import CrossMachineGatewayUnavailable


@pytest.fixture
def _force_local_machine(set_machine_identity) -> str:
    """Sets this unit's identity at the source via set_machine_identity so every
    machine_name() / machine_role() call site sees role=agent-runner, name='local-test'."""
    set_machine_identity(role="agent-runner", name="local-test")
    return "local-test"


class TestRouting:
    def test_machine_eq_local_forwards_to_local_target(
        self, _force_local_machine: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """body.machine == local → spawn is HTTP-uniform: still forwarded, with
        target == local (the co-located runner's ops server over localhost)."""
        captured: dict[str, Any] = {}

        async def _capture_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
            captured["target"] = target
            captured["body"] = body
            return SpawnedAgent(id=777)

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _capture_forward)
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"machine": "local-test"})
        assert resp.status_code == 201
        assert resp.json() == {"id": 777}
        assert captured["target"] == "local-test"

    def test_machine_none_forwards_to_local_target(
        self, _force_local_machine: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """body.machine absent → equivalent to local; still forwarded, target == local."""
        captured: dict[str, Any] = {}

        async def _capture_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
            captured["target"] = target
            captured["body"] = body
            return SpawnedAgent(id=778)

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _capture_forward)
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={})
        assert resp.status_code == 201
        assert captured["target"] == "local-test"

    def test_local_gateway_only_target_400(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A target the registry resolves to gateway-only (no agent-runner
        capability) returns 400 + reason='spawn_target_not_agent_runner' via the
        wire contract (a bare HTTPException would carry no reason and trip the SDK
        into a confusing KeyError), and never forwards. The check reads the
        registry for the local target too — no self-introspection."""
        set_machine_identity(role="gateway", name="gw-only")
        # The registry says the (local) target is gateway-only — overrides the
        # conftest autouse stub that defaults the local machine to agent-runner.
        monkeypatch.setattr("shared.machines.lookup_role", lambda _name: ["gateway"])  # pyright: ignore[reportUnknownArgumentType]
        forwarded: list[str] = []

        async def _should_not_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
            forwarded.append(target)
            return SpawnedAgent(id=0)

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _should_not_forward)
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={})
        assert resp.status_code == 400
        body = resp.json()
        assert body["reason"] == "spawn_target_not_agent_runner"
        assert "agent-runner" in body["detail"]
        assert forwarded == []  # guard fires before any forward

    def test_paused_target_409_no_forward(
        self,
        set_machine_identity,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spawn targeting a PAUSED machine (operator `ava cluster pause`) is
        refused with 409 + reason='machine_paused' before any forward — the
        paused machine is deliberately out of the cluster (agents terminated,
        ops server may be unreachable), so a spawn would fail at dial time
        anyway; the precise wire error is what schedules / peers see."""
        set_machine_identity(role="agent-runner", name="local-test")
        monkeypatch.setattr("shared.machines.lookup_role", lambda _name: ["agent-runner"])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.machines.is_paused", lambda _name: True)  # pyright: ignore[reportUnknownArgumentType]
        forwarded: list[str] = []

        async def _should_not_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
            forwarded.append(target)
            return SpawnedAgent(id=0)

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _should_not_forward)
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"machine": "paused-box"})
        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "machine_paused"
        assert "resume" in body["detail"]
        assert forwarded == []  # guard fires before any forward

    def test_machine_neq_local_forwards(
        self, _force_local_machine: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """body.machine != local → _forward_spawn_to_remote is called, body passthrough."""
        captured: dict[str, Any] = {}

        async def _capture_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
            captured["target"] = target
            captured["body"] = body
            return SpawnedAgent(id=999)

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _capture_forward)
        # The pre-dispatch capability check resolves the target's role; stub it as
        # a runner so the forward proceeds (the lookup itself is exercised by the
        # 404 / no-capability tests).
        monkeypatch.setattr("shared.machines.lookup_role", lambda _name: ["agent-runner"])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.machines.is_paused", lambda _name: False)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            resp = client.post(
                "/api/agents",
                json={"machine": "remote-mac", "spawner": "user"},
            )
        assert resp.status_code == 201
        assert resp.json() == {"id": 999}
        assert captured["target"] == "remote-mac"
        # The forward op is the launch half of the #1236 split — the row was
        # already created by the gateway, so the body carries the new agent id,
        # not the REST request (which has no machine / spawner on the wire op).
        launch_body = captured["body"]
        assert launch_body.agent_id > 0
        assert launch_body.config is None
        assert launch_body.prompt is None  # plain spawn: prompt goes post-launch

    def test_registered_remote_runner_is_forwarded(
        self, _force_local_machine: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registered remote runner is always forwarded to — there is no
        deployment gate on spawn routing. Single box is just the special case
        where the target's ops server is at localhost."""
        captured: dict[str, Any] = {}

        async def _capture_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
            captured["target"] = target
            return SpawnedAgent(id=999)

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _capture_forward)
        monkeypatch.setattr("shared.machines.lookup_role", lambda _name: ["agent-runner"])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.machines.is_paused", lambda _name: False)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"machine": "remote-mac"})
        assert resp.status_code == 201
        assert captured["target"] == "remote-mac"

    def test_machine_not_registered_propagates_404(
        self, _force_local_machine: str, db_conn: psycopg.Connection
    ) -> None:
        """A target not in the machines table → the pre-dispatch lookup_role raises
        MachineNotRegistered → the app's AvaAgentError handler emits 404 + reason
        (no opaque 502 from dialing a non-existent ops server).

        Wire protocol: AvaAgentError subclass → JSONResponse status=cls.http_status,
        body={detail, reason}. MachineNotRegistered.http_status=404.
        """
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"machine": "ghost-mac"})
        assert resp.status_code == 404
        body = resp.json()
        assert body["reason"] == "machine_not_registered"
        assert "ghost-mac" in body["detail"]

    def test_cross_machine_gateway_unavailable_propagates_502(
        self, _force_local_machine: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Target gateway transport error → CrossMachineGatewayUnavailable → 502 + reason."""

        async def _forward_raises(*args: Any, **kw: Any) -> None:
            raise CrossMachineGatewayUnavailable("target unreachable after 3 retries")

        monkeypatch.setattr(app_module, "_forward_spawn_to_remote", _forward_raises)
        monkeypatch.setattr("shared.machines.lookup_role", lambda _name: ["agent-runner"])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.machines.is_paused", lambda _name: False)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            resp = client.post("/api/agents", json={"machine": "remote-mac"})
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"
