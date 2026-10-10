"""Cross-machine terminate forward (`gateway/agents/lifecycle.py:post_agent_terminate`) unit tests —

Both graceful and force requests route to the home runner. It owns the hosted
turn and local execution resources; gateway placement must not choose a local
shortcut for a remote agent.

`TestTerminateOpenTasksHint` covers the advisory `open_tasks` hint the gateway
attaches to the response once the forward returns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.agents import CrossMachineGatewayUnavailable, MachineNotRegistered
from base.db import Database
from gateway.agents import forward as forward_module
from gateway.agents import lifecycle as lifecycle_module
from gateway.app import app


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
        """A local force request is accepted by the same home-runner path."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "local-test")
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "user"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "enqueued"

    def test_remote_graceful_forwards(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """machine != local + force=false → forward, local does not INSERT inbound."""
        captured: dict[str, Any] = {}

        async def _capture_forward(
            agent_id: int, path: str, json_body: dict, *, db: Database, pool: ConnectionPool
        ) -> dict:
            assert db is app.state.db
            assert pool is app.state.db_pool
            captured["agent_id"] = agent_id
            captured["path"] = path
            captured["json_body"] = json_body
            return {"status": "enqueued"}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"message": "retain this note"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
        assert captured["agent_id"] == agent_id
        assert captured["path"] == f"/api/agents/{agent_id}/terminate"
        assert captured["json_body"]["message"] == "retain this note"

    def test_remote_force_forwards(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """machine != local + force=true → forward, local does not touch sessions / pid."""
        captured: dict[str, Any] = {}

        async def _capture_forward(
            agent_id: int, path: str, json_body: dict, *, db: Database, pool: ConnectionPool
        ) -> dict:
            assert db is app.state.db
            assert pool is app.state.db_pool
            captured["json_body"] = json_body
            return {"status": "enqueued"}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "user"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
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
            monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _forward_raises)
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
            monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _forward_raises)
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

    async def _capture_enqueue(_db: object, target: str, path: str, json_body: dict) -> dict:
        captured["target"] = target
        return {"status": "enqueued"}

    with TestClient(app) as client:
        agent_id = client.post("/api/agents", json={}).json()["id"]
        _set_agent_machine(db_conn, agent_id, "stale-wsl")
        monkeypatch.setattr(forward_module, "enqueue_lifecycle", _capture_enqueue)  # pyright: ignore[reportUnknownArgumentType]
        resp = client.post(f"/api/agents/{agent_id}/terminate")
    assert resp.status_code == 200
    assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
    assert captured["target"] == "stale-wsl"


def test_restart_overlay_is_validated_without_gateway_agent_domain(
    _force_local_machine: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart validates its forwarded overlay without constructing agent settings."""
    import base.config as base_config
    from base.config import Settings

    captured: dict[str, Any] = {}

    async def _capture_forward(
        agent_id: int,
        path: str,
        json_body: dict[str, object],
        *,
        db: Database,
        pool: ConnectionPool,
    ) -> dict[str, str]:
        assert db is app.state.db
        assert pool is app.state.db_pool
        captured["agent_id"] = agent_id
        captured["path"] = path
        captured["json_body"] = json_body
        return {"status": "enqueued"}

    monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        agent_id = client.post("/api/agents", json={}).json()["id"]
        _set_agent_machine(db_conn, agent_id, "remote-runner")
        with monkeypatch.context() as profile_patch:
            profile_patch.setattr(base_config, "settings", Settings(profile="gateway"))
            valid = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"config_overlay": {"completion_notice_policy": "hourly"}},
            )
            invalid = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"config_overlay": {"completion_notice_policy": "bogus"}},
            )

    assert valid.status_code == 200
    assert valid.json() == {"status": "enqueued"}
    assert captured["agent_id"] == agent_id
    assert captured["path"] == f"/api/agents/{agent_id}/restart"
    assert captured["json_body"] == {
        "source": "user",
        "config_overlay": {"completion_notice_policy": "hourly"},
    }
    assert invalid.status_code == 422


def _insert_open_task(
    db_conn: psycopg.Connection,
    *,
    owner: int,
    title: str,
    status: str = "in_progress",
    age_minutes: int = 0,
) -> int:
    """Seed one agent_tasks row for `owner`, `age_minutes` old."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, owner, updated_at) "
            "VALUES (%s, '', %s, 'user', %s, now() - make_interval(mins => %s)) RETURNING id",
            (title, status, owner, age_minutes),
        )
        task_id: int = cur.fetchone()[0]  # type: ignore[index]
    db_conn.commit()
    return task_id


class TestTerminateOpenTasksHint:
    """The terminate response carries `open_tasks` — the agent's still-open
    tasks, read at the gateway after the home runner accepts the termination."""

    def test_no_open_tasks_reports_null(
        self, _force_local_machine: str, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "local-test")
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

    def test_open_tasks_reported_newest_first(
        self, _force_local_machine: str, db_conn: psycopg.Connection
    ) -> None:
        """Only in_progress counts; rows come newest first."""
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "local-test")
            _insert_open_task(
                db_conn, owner=agent_id, title="done task", status="done", age_minutes=1
            )
            _insert_open_task(
                db_conn, owner=agent_id, title="cancelled task", status="cancelled", age_minutes=2
            )
            older = _insert_open_task(
                db_conn, owner=agent_id, title="older open task", age_minutes=30
            )
            newer = _insert_open_task(
                db_conn, owner=agent_id, title="newer in-progress task", age_minutes=5
            )
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        hint = resp.json()["open_tasks"]
        assert hint["count"] == 2
        assert hint["more"] == 0
        assert [(task["id"], task["status"]) for task in hint["tasks"]] == [
            (newer, "in_progress"),
            (older, "in_progress"),
        ]
        assert hint["tasks"][0]["title"] == "newer in-progress task"
        assert datetime.fromisoformat(hint["tasks"][0]["updated_at"])

    def test_more_than_five_truncates_to_the_five_newest(
        self, _force_local_machine: str, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "local-test")
            seeded = [
                _insert_open_task(
                    db_conn, owner=agent_id, title=f"open task {i}", age_minutes=10 - i
                )
                for i in range(7)
            ]
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        hint = resp.json()["open_tasks"]
        assert hint["count"] == 7
        assert hint["more"] == 2
        assert [task["id"] for task in hint["tasks"]] == list(reversed(seeded))[:5]


def _seed_shell_ttls(db_conn: psycopg.Connection, agent_id: int, session_ids: list[int]) -> None:
    with db_conn.cursor() as cur:
        for session_id in session_ids:
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now() + interval '1 hour')",
                (agent_id, session_id),
            )
    db_conn.commit()


def _shell_ttl_ids(db_conn: psycopg.Connection, agent_id: int) -> list[int]:
    rows = db_conn.execute(
        "SELECT session_id FROM agent_shell_ttls WHERE agent_id = %s ORDER BY session_id",
        (agent_id,),
    ).fetchall()
    return [row[0] for row in rows]


class TestTerminateShellSessions:
    """`kill_all_shell_sessions` rides to the home runner, whose report comes
    back verbatim in `shell_sessions`; the gateway then drops the TTL rows of
    the sessions it reports killed (the runner holds no DELETE on them)."""

    def test_forwards_the_option_and_drops_killed_sessions_ttl_rows(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        async def _capture_forward(
            agent_id: int, path: str, json_body: dict, *, db: Database, pool: ConnectionPool
        ) -> dict:
            assert db is app.state.db
            assert pool is app.state.db_pool
            captured["json_body"] = json_body
            return {"status": "enqueued", "shell_sessions": {"when": "now", "killed": [0, 1]}}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            _seed_shell_ttls(db_conn, agent_id, [0, 1, 2])
            monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "kill_all_shell_sessions": True},
            )
        assert resp.status_code == 200
        assert resp.json()["shell_sessions"] == {"when": "now", "killed": [0, 1]}
        assert captured["json_body"]["kill_all_shell_sessions"] is True
        # Session 2 is gone too, but not by this kill: the TTL reaper retires it.
        assert _shell_ttl_ids(db_conn, agent_id) == [2]

    def test_at_exit_kill_leaves_ttl_rows_to_the_reaper(
        self,
        _force_local_machine: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _capture_forward(
            agent_id: int, path: str, json_body: dict, *, db: Database, pool: ConnectionPool
        ) -> dict:
            assert db is app.state.db
            assert pool is app.state.db_pool
            return {"status": "enqueued", "shell_sessions": {"when": "at_exit", "killed": []}}

        with TestClient(app) as client:
            agent_id = client.post("/api/agents", json={}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            _seed_shell_ttls(db_conn, agent_id, [0])
            monkeypatch.setattr(lifecycle_module, "forward_to_home_machine", _capture_forward)  # pyright: ignore[reportUnknownArgumentType]
            resp = client.post(
                f"/api/agents/{agent_id}/terminate", json={"kill_all_shell_sessions": True}
            )
        assert resp.status_code == 200
        assert resp.json()["shell_sessions"] == {"when": "at_exit", "killed": []}
        assert _shell_ttl_ids(db_conn, agent_id) == [0]
