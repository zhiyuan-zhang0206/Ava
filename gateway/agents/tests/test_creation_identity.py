"""Lost-response and concurrent agent creation recover the committed birth."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.agents import router as agent_router
from gateway.app import app
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, set_machine_identity):
    set_machine_identity(role="agent-runner", name="local-test")

    async def accept(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", accept)
    with TestClient(app) as value:
        yield value


def test_lost_response_replays_birth_and_first_prompt(
    client: TestClient, db_conn: psycopg.Connection
):
    key = uuid4().hex
    body = {"machine": "local-test", "prompt": "one initial prompt", "prompt_source": "user"}
    first = client.post("/api/agents", json=body, headers={"Idempotency-Key": key})
    retry = client.post("/api/agents", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == retry.status_code == 201
    assert first.json()["id"] == retry.json()["id"]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='chat'",
        (first.json()["id"],),
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM agents_meta WHERE creation_key=%s", (key,)
    ).fetchone() == (1,)
    changed = client.post(
        "/api/agents", json={**body, "prompt": "different"}, headers={"Idempotency-Key": key}
    )
    assert changed.status_code == 409


def test_concurrent_retries_create_one_birth(client: TestClient, db_conn: psycopg.Connection):
    key = uuid4().hex

    def submit(_index: int):
        return client.post(
            "/api/agents", json={"machine": "local-test"}, headers={"Idempotency-Key": key}
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(submit, range(4)))
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert db_conn.execute(
        "SELECT count(*) FROM agents_meta WHERE creation_key=%s", (key,)
    ).fetchone() == (1,)


def test_creation_retry_does_not_resurrect_terminated_agent(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    key = uuid4().hex
    body = {"machine": "local-test"}
    first = client.post("/api/agents", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201
    agent_id = first.json()["id"]
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()

    async def unexpected(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise AssertionError("a creation retry must not launch later terminated work")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unexpected)
    retry = client.post("/api/agents", json=body, headers={"Idempotency-Key": key})
    assert retry.status_code == 201
    assert retry.json()["id"] == agent_id


def test_keyless_creations_remain_distinct(client: TestClient):
    body = {"machine": "local-test"}
    first = client.post("/api/agents", json=body)
    second = client.post("/api/agents", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_creation_replay_precedes_mutable_spawn_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    key = uuid4().hex
    body = {"machine": "local-test"}
    first = client.post("/api/agents", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201

    monkeypatch.setattr("base.cluster.machines.is_paused", lambda _db, _name: True)
    fresh = client.post("/api/agents", json=body)
    assert fresh.status_code == 409
    retry = client.post("/api/agents", json=body, headers={"Idempotency-Key": key})
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]


def test_migration_preserves_existing_keyless_birth(
    client: TestClient, db_conn: psycopg.Connection
):
    response = client.post("/api/agents", json={"machine": "local-test"})
    assert response.status_code == 201
    agent_id = response.json()["id"]
    migration = next(
        (Path(__file__).resolve().parents[3] / "migrations").glob("*_agent-creation-identity.sql")
    )
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("ALTER TABLE agents_meta DROP COLUMN creation_key CASCADE")
        db_conn.execute("ALTER TABLE agents_meta DROP COLUMN creation_request_hash CASCADE")
        db_conn.execute(migration.read_text())
        assert db_conn.execute(
            "SELECT creation_key, creation_request_hash FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == (None, None)
