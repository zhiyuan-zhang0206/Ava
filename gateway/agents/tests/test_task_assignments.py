"""Guarded compound acceptance has one atomic pair and immutable receipt."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config import settings
from gateway.agents import router as agent_router
from gateway.agents.task_assignment import router as task_assignments
from gateway.app import app
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

PATH = "/api/keyed/v1/task-assignments"
HEADERS = {"Idempotency-Key": "compound", "Idempotency-Scope": "principal-v1"}
SECRET = "compound-test-secret"  # noqa: S105 -- isolated test authentication


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, set_machine_identity: Any) -> Iterator[TestClient]:
    set_machine_identity(role="agent-runner", name="local-test")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)

    async def accept(db: object, target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", accept)
    with TestClient(app, headers={"Authorization": f"Bearer {SECRET}"}) as value:
        yield value


@pytest.fixture
def body(db_conn: psycopg.Connection) -> dict[str, Any]:
    db_conn.execute("INSERT INTO machines(name,role) VALUES ('local-test',ARRAY['agent-runner'])")
    db_conn.execute("INSERT INTO agent_presets(name,label,config) VALUES ('coder','Coder','{}')")
    actor = db_conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert actor is not None
    db_conn.execute(
        "INSERT INTO agents_meta(id,spawner,status,machine) VALUES (%s,'user','idling','local-test')",
        (actor[0],),
    )
    parent = db_conn.execute(
        "INSERT INTO agent_tasks(title,description,status,created_by,is_root) VALUES ('Root','','in_progress','system',true) RETURNING id"
    ).fetchone()
    assert parent is not None
    db_conn.commit()
    return {
        "actor_agent_id": actor[0],
        "task": {"title": "One assignment", "description": "Work", "parent": parent[0]},
        "agent": {"machine": "local-test"},
    }


def _counts(conn: psycopg.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM agents),(SELECT count(*) FROM agent_tasks),(SELECT count(*) FROM audit_events),(SELECT count(*) FROM inbound_messages),(SELECT count(*) FROM task_assignment_receipts)"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row


def test_response_loss_and_concurrent_duplicates_return_original_pair(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    before = _counts(db_conn)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(client.post, PATH, json=body, headers=HEADERS) for _ in range(3)]
        responses = [future.result() for future in futures]
    for response in responses:
        assert response.status_code == 201, response.text
        for field in ("task", "agent_id", "launch_attempt_id"):
            assert response.json()[field] == first.json()[field]
    assert _counts(db_conn) == before
    changed = {**body, "task": {**body["task"], "description": "Different"}}
    assert client.post(PATH, json=changed, headers=HEADERS).status_code == 409


def test_producer_failure_rolls_back_entire_pair(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _counts(db_conn)

    def refuse(*args: Any) -> None:
        raise RuntimeError("receipt failure after birth/task/audit/inbound")

    monkeypatch.setattr(task_assignments, "save_assignment", refuse)
    with pytest.raises(RuntimeError, match="receipt failure"):
        client.post(PATH, json=body, headers=HEADERS)
    assert _counts(db_conn) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_agent_id": True},
        {"actor_agent_id": 0},
        {"unknown": 1},
        {"agent": {"fork_from": 1}},
        {"task": {"title": "X", "description": "", "parent": True}},
        {"task": {"title": "X", "description": "", "parent": 1, "priority": "unknown"}},
    ],
)
def test_invalid_raw_input_cannot_create_pair(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any], changes: dict[str, Any]
) -> None:
    before = _counts(db_conn)
    assert client.post(PATH, json={**body, **changes}, headers=HEADERS).status_code == 422
    assert _counts(db_conn) == before


def test_snapshot_replays_after_task_and_birth_metadata_deleted(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    accepted = first.json()
    db_conn.execute("DELETE FROM agent_tasks WHERE id=%s", (accepted["task"]["id"],))
    db_conn.execute("DELETE FROM inbound_messages WHERE agent_id=%s", (accepted["agent_id"],))
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (accepted["agent_id"],))
    db_conn.execute("DELETE FROM machines WHERE name='local-test'")
    db_conn.commit()
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == accepted["task"]
    assert replay.json()["agent_id"] == accepted["agent_id"]
    assert replay.json()["launch"] is None


def test_replay_skips_preset_and_changed_default_policy(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.agents.tasks.priority import DEFAULT_REMIND_INTERVAL_SECONDS, Priority

    db_conn.execute(
        "INSERT INTO agent_presets(name,label,config) VALUES ('compound-policy','Policy','{}')"
    )
    db_conn.commit()
    body["agent"]["config"] = {"preset": "compound-policy"}
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    original = first.json()
    db_conn.execute("DELETE FROM agent_presets WHERE name='compound-policy'")
    db_conn.execute(
        "UPDATE agent_tasks SET title='Renamed',status='done' WHERE id=%s",
        (original["task"]["id"],),
    )
    db_conn.commit()
    monkeypatch.setitem(DEFAULT_REMIND_INTERVAL_SECONDS, Priority.P2, 123)
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == original["task"]
    explicit = {
        **body,
        "task": {
            **body["task"],
            "remind_interval_seconds": original["task"]["remind_interval_seconds"],
        },
    }
    assert client.post(PATH, json=explicit, headers=HEADERS).status_code == 409


def test_launch_failure_retains_accepted_pair_and_original_attempt(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[UUID | None] = []

    async def unavailable(db: object, target: str, launch: LaunchAgentRequest) -> SpawnedAgent:
        attempts.append(launch.launch_attempt_id)
        raise RuntimeError("runner unavailable")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unavailable)
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    original = first.json()
    assert original["launch"] is None and original["launch_failure"]
    assert original["retry_launch_path"].endswith("/retry-launch")
    before = _counts(db_conn)
    again = client.post(PATH, json=body, headers=HEADERS)
    assert again.status_code == 201
    assert again.json()["task"] == original["task"]
    assert again.json()["launch_attempt_id"] == original["launch_attempt_id"]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert _counts(db_conn) == before


def test_historical_birth_snapshot_does_not_override_current_launch_owner(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    original = first.json()
    history = db_conn.execute("SELECT birth_snapshot FROM task_assignment_receipts").fetchone()
    assert history is not None
    db_conn.execute(
        "UPDATE agents_meta SET machine='operator-new-machine',config_overlay='{}' WHERE id=%s",
        (original["agent_id"],),
    )
    db_conn.commit()
    seen: list[tuple[str, dict[str, object] | None, str]] = []

    async def observe(db: object, target: str, launch: LaunchAgentRequest) -> SpawnedAgent:
        seen.append((target, launch.config, str(launch.launch_attempt_id)))
        return SpawnedAgent(id=launch.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", observe)
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201
    assert seen == [("operator-new-machine", {}, original["launch_attempt_id"])]
    assert (
        db_conn.execute("SELECT birth_snapshot FROM task_assignment_receipts").fetchone() == history
    )
    assert history[0]["machine"] == "local-test"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "x"},
        {"Idempotency-Key": "", "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "legacy"},
    ],
)
def test_missing_or_wrong_admission_headers_cannot_write(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any], headers: dict[str, str]
) -> None:
    before = _counts(db_conn)
    assert client.post(PATH, json=body, headers=headers).status_code == 422
    assert _counts(db_conn) == before


def test_revoked_and_unverified_principals_cannot_replay(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 201
    before = _counts(db_conn)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated-compound-secret")
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 401
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 422
    assert _counts(db_conn) == before


def test_cookie_and_bearer_share_verified_subject(client: TestClient, body: dict[str, Any]) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201
    assert client.post("/api/auth/login", json={"password": SECRET}).status_code == 200
    client.headers.pop("Authorization")
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201
    assert replay.json()["task"] == first.json()["task"]


def test_postcommit_launch_observation_failure_preserves_pair(
    client: TestClient, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any):
        raise psycopg.OperationalError("current-state read unavailable")

    monkeypatch.setattr(task_assignments, "find_creation", unavailable)
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    assert first.json()["launch"] is None
    assert "OperationalError" in first.json()["launch_failure"]
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201
    assert replay.json()["task"] == first.json()["task"]
    assert replay.json()["launch_attempt_id"] == first.json()["launch_attempt_id"]


def test_corrupt_snapshot_fails_without_recreating_mutable_state(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 201
    before = _counts(db_conn)
    db_conn.execute(
        "UPDATE task_assignment_receipts SET result=jsonb_set(result,'{task}',(result->'task')-'priority')"
    )
    db_conn.commit()
    with pytest.raises(ValueError, match="missing or unknown"):
        client.post(PATH, json=body, headers=HEADERS)
    assert _counts(db_conn) == before
