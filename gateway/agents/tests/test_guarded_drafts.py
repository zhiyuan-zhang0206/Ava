"""Guarded raw draft identity freezes the first birth despite later mutable state."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from gateway.agents import router as agent_router
from gateway.agents.tests.test_guarded_creation import HEADERS
from gateway.agents.tests.test_guarded_creation import client as client
from gateway.extensions import packages
from gateway.routers import guide
from gateway.schedules import router as schedules
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process

CASES = [
    ("guide", {"nl": "one intent"}),
    ("schedules", {"nl": "one intent"}),
    ("packages", {"nl": "one intent", "kind": "skill"}),
]


@pytest.mark.parametrize("surface,body", CASES)
def test_raw_intent_replay_survives_defaults_and_prompt_changes(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    body: dict[str, str],
) -> None:
    path = f"/api/keyed/v1/{surface}/draft"
    first = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 200, first.text
    agent_id = first.json()["agent_id"]
    stored = db_conn.execute(
        "SELECT launch_attempt_id, prompt_inbound_id, prompt_content FROM agent_creation_snapshots"
    ).fetchone()
    assert stored is not None
    for module in (guide, schedules, packages):
        monkeypatch.setattr(module, "machine_name", lambda: "no-longer-registered")
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (stored[0],)
    assert db_conn.execute(
        "SELECT id, content FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchall() == [(stored[1], stored[2])]
    assert client.post(path, json={**body, "nl": "changed"}, headers=HEADERS).status_code == 409
    if surface == "packages":
        assert (
            client.post(path, json={**body, "kind": "plugin"}, headers=HEADERS).status_code == 409
        )


@pytest.mark.parametrize("mutation", ["rotated", "admitted", "terminated", "deleted"])
def test_historical_snapshot_never_wakes_later_work(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path = "/api/keyed/v1/guide/draft"
    first = client.post(path, json={"nl": "one intent"}, headers=HEADERS)
    agent_id = first.json()["agent_id"]
    if mutation == "rotated":
        db_conn.execute(
            "UPDATE agents_meta SET last_launch_attempt_id=%s WHERE id=%s", (uuid4(), agent_id)
        )
    elif mutation == "admitted":
        db_conn.execute(
            "UPDATE agents_meta SET last_admission_at=now(), last_admission_outcome='admitted' WHERE id=%s",
            (agent_id,),
        )
    elif mutation == "terminated":
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    else:
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent_id,))
    db_conn.commit()

    async def unexpected(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise AssertionError("historical draft must not wake later work")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unexpected)
    replay = client.post(path, json={"nl": "one intent"}, headers=HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


def test_concurrent_same_intent_commits_one_prompt(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    def submit(_index: int):
        return client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)

    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(submit, range(3)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["agent_id"] for response in responses}) == 1
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
    fresh = client.post(
        "/api/keyed/v1/guide/draft",
        json={"nl": "one"},
        headers={**HEADERS, "Idempotency-Key": "new-intent"},
    )
    assert fresh.json()["agent_id"] != responses[0].json()["agent_id"]


def test_snapshot_failure_rolls_back_birth_and_prompt(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected snapshot failure")

    monkeypatch.setattr("ops.agents.birth_transaction.record_creation_snapshot", fail)
    with pytest.raises(RuntimeError, match="injected snapshot"):
        client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    for table in ("agents", "inbound_messages", "agent_creation_snapshots"):
        assert db_conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608


@pytest.mark.parametrize("surface,body", CASES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Scope": "unknown"},
        {**HEADERS, "Idempotency-Key": "x" * 129},
    ],
)
def test_invalid_admission_has_zero_effect(
    client: TestClient,
    db_conn: psycopg.Connection,
    surface: str,
    body: dict[str, str],
    headers: dict[str, str],
) -> None:
    assert (
        client.post(f"/api/keyed/v1/{surface}/draft", json=body, headers=headers).status_code == 422
    )
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


@pytest.mark.parametrize(
    "source_router,surface,body",
    [
        (guide.router, "guide", {"nl": "one"}),
        (schedules.router, "schedules", {"nl": "one"}),
        (packages.router, "packages", {"nl": "one", "kind": "skill"}),
    ],
)
def test_old_routing_cannot_execute_guarded_intent(
    client: TestClient,
    db_conn: psycopg.Connection,
    source_router: APIRouter,
    surface: str,
    body: dict[str, str],
) -> None:
    old = FastAPI()
    router = APIRouter()
    for route in source_router.routes:
        if isinstance(route, APIRoute) and not route.path.startswith("/api/keyed/"):
            router.routes.append(route)
    old.include_router(router)
    with TestClient(old) as value:
        assert value.post(
            f"/api/keyed/v1/{surface}/draft", json=body, headers=HEADERS
        ).status_code in (404, 405)
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


def test_lost_launch_ack_keeps_first_attempt_and_first_prompt(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[LaunchAgentRequest] = []

    async def lost_once(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        calls.append(body)
        if len(calls) == 1:
            raise TimeoutError("native wake completed but acknowledgement was lost")
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", lost_once)
    first = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    assert first.status_code == 502, first.text
    retry = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    assert retry.status_code == 200, retry.text
    assert len(calls) == 2
    assert calls[0].launch_attempt_id == calls[1].launch_attempt_id
    assert calls[0].agent_id == calls[1].agent_id == retry.json()["agent_id"]
    assert all("prompt" not in call.model_dump() for call in calls)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_missing_snapshot_fails_closed_without_backfill(
    client: TestClient,
    db_conn: psycopg.Connection,
) -> None:
    path = "/api/keyed/v1/guide/draft"
    assert client.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 200
    db_conn.execute("DELETE FROM agent_creation_snapshots")
    db_conn.commit()
    assert client.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 409
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (0,)


def test_replay_reauthenticates_and_unverified_posture_cannot_create(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings

    path = "/api/keyed/v1/guide/draft"
    assert client.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 200
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated-draft-secret")
    assert client.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 401
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    assert client.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)


def test_deliberate_retry_rotates_pointer_but_draft_replays_original_attempt(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.app import app
    from ops.agents.creation_identity import find_creation
    from ops.lifecycle.launch import _validate_launch_row

    first = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    agent_id = first.json()["agent_id"]
    row = db_conn.execute(
        "SELECT creation_key, request_hash, launch_attempt_id FROM agent_creation_snapshots"
    ).fetchone()
    assert row is not None
    key, digest, attempt = row

    async def reconcile(*args: object, **kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr("ops.cluster.rpc.dispatch_to_machine", reconcile)
    retry = client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": str(attempt)},
        headers={**HEADERS, "Idempotency-Key": "deliberate-retry"},
    )
    assert retry.status_code == 200, retry.text
    replacement = retry.json()["launch_attempt_id"]
    assert replacement != str(attempt)
    original = find_creation(db_conn, key, digest, immutable_snapshot=True)
    assert original is not None and original.launch_attempt_id == attempt
    assert not original.launch_pending
    # Preserve this existing projection for legacy/MCP callers; it is a separate
    # known recovery gap, not an immutable original-attempt receipt.
    legacy = find_creation(db_conn, key, digest)
    assert legacy is not None and str(legacy.launch_attempt_id) == replacement
    with pytest.raises(ValueError, match="launch"):
        _validate_launch_row(
            app.state.db_pool, LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=attempt)
        )

    async def unexpected(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise AssertionError("original draft replay cannot launch replacement attempt")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unexpected)
    replay = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_concurrent_changed_raw_intents_conflict_in_birth_transaction(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Barrier
    from typing import Any

    original = agent_router.create_agent_row
    gate = Barrier(2)

    def race(*args: Any, **kwargs: Any):
        gate.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_router, "create_agent_row", race)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                client.post, "/api/keyed/v1/guide/draft", json={"nl": text}, headers=HEADERS
            )
            for text in ("one", "different")
        ]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
