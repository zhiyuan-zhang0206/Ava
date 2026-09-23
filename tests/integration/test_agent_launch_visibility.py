"""Committed launch failure and same-identity recovery at the HTTP boundary."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from tests.gateway.test_agents_endpoints import _inbound_rows


def _assert_failed_birth_visible(client: TestClient, body: dict[str, Any], agent_id: int) -> None:
    assert body["retryable"] is False
    assert body["state"]["status"] == "idling"
    assert body["state"]["availability"]["reason"] == "launch_unreachable"
    assert body["retry_launch_path"] == f"/api/agents/{agent_id}/retry-launch"
    assert (
        client.get(f"/api/agents/{agent_id}").json()["availability"]["reason"]
        == "launch_unreachable"
    )
    roster = client.get("/api/agents/roster").json()["agents"]
    assert (
        next(row for row in roster if row["agent_id"] == agent_id)["availability"]["reason"]
        == "launch_unreachable"
    )


def test_failed_plain_launch_persists_prompt_and_retry_reuses_identity(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from gateway.routers import agents as route
    from gateway.routers.agents_forward import LaunchForwardError
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
    from shared.agent_observation import AvailabilityReason

    attempts: list[LaunchAgentRequest] = []

    async def _fail(_target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        attempts.append(body)
        raise LaunchForwardError(AvailabilityReason.LAUNCH_UNREACHABLE, "runner offline")

    monkeypatch.setattr(route, "_forward_spawn_to_remote", _fail)
    with TestClient(app) as client:
        failed = client.post("/api/agents", json={"prompt": "Do the task", "prompt_source": "user"})
        assert failed.status_code == 502
        body = failed.json()
        agent_id = body["agent_id"]
        _assert_failed_birth_visible(client, body, agent_id)
        assert _inbound_rows(db_conn, agent_id) == [("Do the task", "chat", "user")]

        async def _succeed(_target: str, retry: LaunchAgentRequest) -> SpawnedAgent:
            attempts.append(retry)
            return SpawnedAgent(id=retry.agent_id)

        monkeypatch.setattr(route, "_forward_spawn_to_remote", _succeed)
        repaired = client.post(body["retry_launch_path"])
        assert repaired.status_code == 200
        assert repaired.json()["id"] == agent_id
        assert attempts[0].launch_attempt_id != attempts[1].launch_attempt_id
        assert attempts[1].prompt is None
        assert _inbound_rows(db_conn, agent_id) == [("Do the task", "chat", "user")]
        assert (
            client.get(f"/api/agents/{agent_id}").json()["availability"]["reason"]
            != "launch_unreachable"
        )


def test_retry_launch_rejects_non_idling_agent_without_rotating_attempt(
    db_conn: psycopg.Connection,
) -> None:
    with TestClient(app) as client:
        created = client.post("/api/agents", json={})
        assert created.status_code == 201
        agent_id = created.json()["id"]
        with db_conn.cursor() as cur:
            cur.execute("SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,))
            original_attempt_row = cur.fetchone()
            assert original_attempt_row is not None
            original_attempt = original_attempt_row[0]
            cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (agent_id,))
        db_conn.commit()

        response = client.post(f"/api/agents/{agent_id}/retry-launch")
        assert response.status_code == 409
        assert response.json()["detail"] == (
            f"agent {agent_id} cannot retry launch in status running"
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,))
            retry_attempt_row = cur.fetchone()
            assert retry_attempt_row is not None
            assert retry_attempt_row[0] == original_attempt


def test_retry_launch_returns_404_for_missing_agent(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM agents_meta")
        missing_id_row = cur.fetchone()
        assert missing_id_row is not None
        missing_id = missing_id_row[0]
    with TestClient(app) as client:
        response = client.post(f"/api/agents/{missing_id}/retry-launch")
    assert response.status_code == 404
    assert response.json()["reason"] == "agent_not_found"


def test_failed_launch_state_write_outage_keeps_committed_id_retriable(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from gateway.routers import agents as route
    from gateway.routers.agents_forward import LaunchForwardError
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
    from shared.agent_observation import AvailabilityReason

    async def _fail(_target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise LaunchForwardError(AvailabilityReason.LAUNCH_UNREACHABLE, "runner offline")

    def _write_outage(*_args: object) -> None:
        raise OSError("database unavailable")

    monkeypatch.setattr(route, "_forward_spawn_to_remote", _fail)
    monkeypatch.setattr(route, "_mark_launch_failure", _write_outage)
    with TestClient(app) as client:
        failed = client.post("/api/agents", json={"prompt": "Keep me", "prompt_source": "user"})
        assert failed.status_code == 502
        body = failed.json()
        assert body["agent_id"] > 0
        assert body["state"] == {"status": "unknown", "availability": None}
        assert _inbound_rows(db_conn, body["agent_id"]) == [("Keep me", "chat", "user")]

        async def _succeed(_target: str, retry: LaunchAgentRequest) -> SpawnedAgent:
            return SpawnedAgent(id=retry.agent_id)

        monkeypatch.setattr(route, "_forward_spawn_to_remote", _succeed)
        assert client.post(f"/api/agents/{body['agent_id']}/retry-launch").status_code == 200


def test_failed_fork_launch_keeps_marker_and_prompt_in_one_birth(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from gateway.routers import agents as route
    from gateway.routers.agents_forward import LaunchForwardError
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
    from shared.agent_observation import AvailabilityReason

    with TestClient(app) as client:
        source = client.post("/api/agents", json={}).json()["id"]
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
                "checkpoint, metadata) VALUES (%s, 'fork-base', NULL, '{}'::jsonb, '{}'::jsonb)",
                (str(source),),
            )
        db_conn.commit()

        async def _fail(_target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
            raise LaunchForwardError(AvailabilityReason.LAUNCH_UNREACHABLE, "runner offline")

        monkeypatch.setattr(route, "_forward_spawn_to_remote", _fail)
        failed = client.post(
            "/api/agents",
            json={"fork_from": source, "prompt": "Continue here", "prompt_source": "user"},
        )
    assert failed.status_code == 502
    agent_id = failed.json()["agent_id"]
    assert _inbound_rows(db_conn, agent_id) == [
        ("", "fork", f"agent:{source}"),
        ("Continue here", "chat", "user"),
    ]


def test_admission_winning_dispatch_failure_returns_accepted_receipt(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from gateway.routers import agents as route
    from gateway.routers.agents_forward import LaunchForwardError
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
    from shared.agent_observation import AvailabilityReason

    async def _admit_then_fail(_target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET last_admission_outcome='admitted', "
                "last_admission_at=clock_timestamp() WHERE id=%s",
                (body.agent_id,),
            )
        db_conn.commit()
        raise LaunchForwardError(AvailabilityReason.LAUNCH_UNREACHABLE, "response lost")

    monkeypatch.setattr(route, "_forward_spawn_to_remote", _admit_then_fail)
    with TestClient(app) as client:
        response = client.post("/api/agents", json={"prompt": "Stay", "prompt_source": "user"})
    assert response.status_code == 201
    agent_id = response.json()["id"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT last_launch_failure_reason FROM agents_meta WHERE id=%s",
            (agent_id,),
        )
        assert cur.fetchone() == (None,)
    assert _inbound_rows(db_conn, agent_id) == [("Stay", "chat", "user")]


def test_first_prompt_insert_failure_rolls_back_agent_row(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from ops import agent_spawn
    from shared.machine import machine_name

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents_meta")
        before = cur.fetchone()

    def _fail_insert(*_args: object) -> int:
        raise RuntimeError("prompt insert refused")

    monkeypatch.setattr(agent_spawn, "insert_spawn_prompt_in_transaction", _fail_insert)
    with pytest.raises(RuntimeError, match="prompt insert refused"):
        agent_spawn.create_agent_row(machine=machine_name(), prompt="Work", prompt_source="user")
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents_meta")
        assert cur.fetchone() == before
