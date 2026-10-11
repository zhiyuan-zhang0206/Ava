"""Lost-response and concurrent agent creation recover the committed birth."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.agents import router as agent_router
from gateway.app import app
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from tests.fixtures.gateway_config import spawn_via_http


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, set_machine_identity):
    set_machine_identity(role="agent-runner", name="local-test")

    async def accept(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", accept)
    with TestClient(app) as value:
        yield value


def _snapshot_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone()
    assert row is not None
    return row[0]


def test_lost_response_replays_birth_and_first_prompt(
    client: TestClient, db_conn: psycopg.Connection
):
    key = uuid4().hex
    body = {"machine": "local-test", "prompt": "one initial prompt", "prompt_source": "user"}
    first = spawn_via_http(client, body, key=key)
    retry = spawn_via_http(client, body, key=key)
    assert first.status_code == retry.status_code == 201
    assert first.json()["id"] == retry.json()["id"]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='chat'",
        (first.json()["id"],),
    ).fetchone() == (1,)
    assert _snapshot_count(db_conn) == 1
    changed = spawn_via_http(client, {**body, "prompt": "different"}, key=key)
    assert changed.status_code == 409


def test_concurrent_retries_create_one_birth(client: TestClient, db_conn: psycopg.Connection):
    key = uuid4().hex

    def submit(_index: int):
        return spawn_via_http(client, {"machine": "local-test"}, key=key)

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(submit, range(4)))
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert _snapshot_count(db_conn) == 1
    assert db_conn.execute("SELECT count(*) FROM agents_meta").fetchone() == (1,)


def test_creation_retry_does_not_resurrect_terminated_agent(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    key = uuid4().hex
    body = {"machine": "local-test"}
    first = spawn_via_http(client, body, key=key)
    assert first.status_code == 201
    agent_id = first.json()["id"]
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()

    async def unexpected(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise AssertionError("a creation retry must not launch later terminated work")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unexpected)
    retry = spawn_via_http(client, body, key=key)
    assert retry.status_code == 201
    assert retry.json()["id"] == agent_id


def test_distinct_keys_create_distinct_births(client: TestClient, db_conn: psycopg.Connection):
    body = {"machine": "local-test"}
    first = spawn_via_http(client, body)
    second = spawn_via_http(client, body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert _snapshot_count(db_conn) == 2


def test_creation_replay_precedes_mutable_spawn_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    key = uuid4().hex
    body = {"machine": "local-test"}
    first = spawn_via_http(client, body, key=key)
    assert first.status_code == 201

    def paused(_db: object, _name: str) -> bool:
        return True

    monkeypatch.setattr("base.cluster.machines.is_paused", paused)
    fresh = spawn_via_http(client, body)
    assert fresh.status_code == 409
    retry = spawn_via_http(client, body, key=key)
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
