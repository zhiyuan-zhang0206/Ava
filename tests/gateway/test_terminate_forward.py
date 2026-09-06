"""Cross-machine terminate forward (`gateway/routers/agents_lifecycle.py:post_agent_terminate`) unit tests —

force=true must forward to the home machine (kill-session / os.kill are physical
local operations). Graceful (force=false) also forwards because zombie detection via
`process_alive` is meaningless for cross-machine PIDs. Forwarding keeps both paths
on the home machine, keeping the logic symmetric.
"""

from __future__ import annotations

from subprocess import CompletedProcess
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
            "UPDATE agents_meta SET machine = %s WHERE id = %s",
            (machine, agent_id),
        )
    db_conn.commit()


class TestTerminateRouting:
    def test_local_graceful_takes_local_path(
        self, _force_local_machine: str, db_conn: psycopg.Connection
    ) -> None:
        """machine == local + force=false → takes local graceful path, INSERT terminate inbound."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "local-test")
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "enqueued"
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT kind FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'",
                (agent_id,),
            )
            assert cur.fetchone() is not None

    def test_local_force_takes_local_path(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """machine == local + force=true → takes local force-kill path (kill-session + os.kill stub)."""
        import subprocess as _subprocess

        monkeypatch.setattr(
            _subprocess,
            "run",
            lambda *_a, **_kw: CompletedProcess(args=[], returncode=0),  # pyright: ignore[reportUnknownArgumentType]
        )
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "local-test")
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "user"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "force_killed"

    def test_remote_graceful_forwards(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """machine != local + force=false → forward, local does not INSERT inbound."""
        captured: dict[str, Any] = {}

        async def _capture_forward(agent_id: int, path: str, json_body: dict) -> dict:
            captured["agent_id"] = agent_id
            captured["path"] = path
            captured["json_body"] = json_body
            return {"status": "enqueued"}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued"}
        assert captured["agent_id"] == agent_id
        assert captured["path"] == f"/api/agents/{agent_id}/terminate"

    def test_remote_force_forwards(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """machine != local + force=true → forward, local does not touch sessions / pid."""
        captured: dict[str, Any] = {}

        async def _capture_forward(agent_id: int, path: str, json_body: dict) -> dict:
            captured["json_body"] = json_body
            return {"status": "force_killed"}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "user"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "force_killed"}
        assert captured["json_body"]["force"] is True
        assert captured["json_body"]["source"] == "user"

    def test_machine_not_registered_propagates_404(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _forward_raises(*args: Any, **kw: Any) -> None:
            raise MachineNotRegistered("machine 'remote-mac' not in machines table")

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _forward_raises)
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 404
        assert resp.json()["reason"] == "machine_not_registered"

    def test_cross_machine_gateway_unavailable_propagates_502(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _forward_raises(*args: Any, **kw: Any) -> None:
            raise CrossMachineGatewayUnavailable("target unreachable after 3 retries")

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "_forward_to_home_machine", _forward_raises)
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"

    def test_nonexistent_agent_404(self, _force_local_machine: str) -> None:
        """Nonexistent → helper itself raises AgentNotFound (404)."""
        with TestClient(app) as client:
            resp = client.post("/api/agents/9999/terminate")
        assert resp.status_code == 404


def test_remote_home_machine_is_forwarded(
    _force_local_machine: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote agents_meta.machine row is always forwarded to that host's ops
    server — there is no deployment gate on the lifecycle path. Single box is
    just the special case where the ops server lives at localhost."""
    captured: dict[str, Any] = {}

    async def _capture_enqueue(target: str, path: str, json_body: dict) -> dict:
        captured["target"] = target
        return {"status": "enqueued"}

    with TestClient(app) as client:
        agent_id = client.post("/api/agents", json={}).json()["id"]
        _set_agent_machine(db_conn, agent_id, "stale-wsl")
        monkeypatch.setattr(forward_module, "_enqueue_lifecycle", _capture_enqueue)  # pyright: ignore[reportUnknownArgumentType]
        resp = client.post(f"/api/agents/{agent_id}/terminate")
    assert resp.status_code == 200
    assert resp.json() == {"status": "enqueued"}
    assert captured["target"] == "stale-wsl"
