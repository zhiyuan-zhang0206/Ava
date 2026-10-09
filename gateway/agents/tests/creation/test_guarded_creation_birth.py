"""Guarded plain creation retains its original birth across mutable launch state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, LiteralString
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from gateway.agents import router as agent_router
from gateway.agents.tests.creation.test_guarded_creation import HEADERS, PATH
from gateway.agents.tests.creation.test_guarded_creation import client as client
from gateway.http.auth.request_principal import AuthPrincipal, principal_key
from ops.agents.creation_identity import creation_request_hash, find_creation
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process

BODY = {"machine": "local-test", "prompt": "one goal", "prompt_source": "user"}


def _snapshot(conn: psycopg.Connection) -> tuple[str, str, UUID]:
    row = conn.execute(
        "SELECT creation_key, request_hash, launch_attempt_id FROM agent_creation_snapshots"
    ).fetchone()
    assert row is not None
    return row


def _no_revalidation(*args: object, **kwargs: object) -> None:
    raise AssertionError("retained birth cannot repeat mutable preflight")


@pytest.mark.parametrize("mutation", ["admitted", "terminated", "deleted", "placed"])
def test_historical_birth_never_wakes_later_work(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    first = client.post(PATH, json=BODY, headers=HEADERS)
    assert first.status_code == 201, first.text
    agent_id = first.json()["id"]
    statements: dict[str, LiteralString] = {
        "admitted": "UPDATE agents_meta SET last_admission_at=now(), last_admission_outcome='admitted' WHERE id=%s",
        "terminated": "UPDATE agents_meta SET status='terminated' WHERE id=%s",
        "deleted": "DELETE FROM agents_meta WHERE id=%s",
        "placed": "UPDATE agents_meta SET machine='later-placement' WHERE id=%s",
    }
    db_conn.execute(sql.SQL(statements[mutation]), (agent_id,))
    db_conn.commit()
    monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", _no_revalidation)

    async def unexpected(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise AssertionError("historical creation cannot wake later work")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unexpected)
    replay = client.post(PATH, json=BODY, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == agent_id
    assert replay.json()["accepted"] is True
    assert replay.json()["execution_observed"] is False
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_original_manifest_precedes_mutable_config_and_defaults(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Omitted server-side placement remains the same semantic request. A later
    # default or operator overlay must not redefine the committed launch manifest.
    body = {"prompt": "one goal", "prompt_source": "user"}
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    agent_id = first.json()["id"]
    stored = db_conn.execute(
        "SELECT machine, config_overlay, birth_config, launch_attempt_id "
        "FROM agent_creation_snapshots"
    ).fetchone()
    assert stored is not None
    changed = {"inject_skills": ["operator-added"]}
    db_conn.execute(
        "UPDATE agents_meta SET config_overlay=%s WHERE id=%s", (Jsonb(changed), agent_id)
    )
    db_conn.commit()
    monkeypatch.setattr(agent_router, "machine_name", lambda: "new-default")
    monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", _no_revalidation)
    calls: list[tuple[str, LaunchAgentRequest]] = []

    async def accept(_db: object, target: str, launch: LaunchAgentRequest) -> SpawnedAgent:
        calls.append((target, launch))
        return SpawnedAgent(id=launch.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", accept)
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert len(calls) == 1
    target, launch = calls[0]
    assert (target, launch.config, launch.birth_config, launch.launch_attempt_id) == stored
    assert db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (changed,)
    key, digest, _ = _snapshot(db_conn)
    expected = creation_request_hash(SpawnAgentRequest.model_validate(body).model_dump(mode="json"))
    assert digest == expected
    assert key == principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "intent")


def test_original_key_cannot_dispatch_deliberate_replacement_attempt(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.app import app
    from ops.lifecycle.launch import _validate_launch_row

    first = client.post(PATH, json=BODY, headers=HEADERS)
    assert first.status_code == 201, first.text
    agent_id = first.json()["id"]
    key, digest, original = _snapshot(db_conn)

    async def reconcile(*args: object, **kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr("ops.cluster.rpc.dispatch_to_machine", reconcile)
    retry = client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": str(original)},
        headers={**HEADERS, "Idempotency-Key": "deliberate-retry"},
    )
    assert retry.status_code == 200, retry.text
    replacement = UUID(retry.json()["launch_attempt_id"])
    assert replacement != original
    _validate_launch_row(
        app.state.db_pool, LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=replacement)
    )
    with pytest.raises(ValueError, match="stale"):
        _validate_launch_row(
            app.state.db_pool, LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=original)
        )
    receipt = find_creation(db_conn, key, digest, immutable_snapshot=True)
    assert (
        receipt is not None and receipt.launch_attempt_id == original and not receipt.launch_pending
    )

    async def unexpected(_db: object, _target: str, _body: LaunchAgentRequest) -> SpawnedAgent:
        raise AssertionError("old creation cannot dispatch a later retry attempt")

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", unexpected)
    replay = client.post(PATH, json=BODY, headers=HEADERS)
    assert replay.status_code == 201 and replay.json()["id"] == agent_id
    assert db_conn.execute("SELECT last_launch_attempt_id FROM agents_meta").fetchone() == (
        replacement,
    )
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_prior_guarded_key_without_snapshot_fails_before_effects(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = client.post("/api/agents", json=BODY, headers=HEADERS)
    assert legacy.status_code == 201, legacy.text
    prior_key = principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "intent")
    db_conn.execute(
        "UPDATE agents_meta SET creation_key=%s WHERE id=%s", (prior_key, legacy.json()["id"])
    )
    db_conn.commit()
    monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", _no_revalidation)
    response = client.post(PATH, json=BODY, headers=HEADERS)
    assert response.status_code == 409 and "snapshot is unavailable" in response.text
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (0,)


def test_snapshot_fault_rolls_back_then_same_key_accepts_fresh_birth(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.agents import birth_transaction

    original = birth_transaction.record_creation_snapshot

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected snapshot failure")

    monkeypatch.setattr(birth_transaction, "record_creation_snapshot", fail)
    with pytest.raises(RuntimeError, match="injected snapshot"):
        client.post(PATH, json=BODY, headers=HEADERS)
    for statement in (
        "SELECT count(*) FROM agents",
        "SELECT count(*) FROM inbound_messages",
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'",
        "SELECT count(*) FROM agent_creation_snapshots",
    ):
        assert db_conn.execute(statement).fetchone() == (0,)
    monkeypatch.setattr(birth_transaction, "record_creation_snapshot", original)
    accepted = client.post(PATH, json=BODY, headers=HEADERS)
    assert accepted.status_code == 201, accepted.text
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


@pytest.mark.parametrize("different", [False, True])
def test_same_key_birth_transaction_freezes_concurrent_winner(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    different: bool,
) -> None:
    original = agent_router.create_agent_row
    gate = Barrier(2)

    def race(*args: Any, **kwargs: Any) -> Any:
        gate.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_router, "create_agent_row", race)
    bodies = [BODY, {**BODY, "prompt": "another goal"} if different else BODY]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(client.post, PATH, json=body, headers=HEADERS) for body in bodies
        ]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == (
        [201, 409] if different else [201, 201]
    )
    successful = next(response for response in responses if response.status_code == 201)
    row = db_conn.execute(
        "SELECT agent_id, request_hash, launch_attempt_id, prompt_content FROM agent_creation_snapshots"
    ).fetchone()
    assert row is not None and row[0] == successful.json()["id"]
    assert row[1] == creation_request_hash(
        SpawnAgentRequest.model_validate(bodies[responses.index(successful)]).model_dump(
            mode="json"
        )
    )
    assert db_conn.execute("SELECT last_launch_attempt_id FROM agents_meta").fetchone() == (row[2],)
    assert db_conn.execute("SELECT content FROM inbound_messages").fetchone() == (row[3],)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'"
    ).fetchone() == (1,)


def test_native_launch_ack_loss_recovers_same_attempt(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.app import app
    from ops.lifecycle.launch import _validate_launch_row

    calls: list[LaunchAgentRequest] = []

    async def lost_once(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        _validate_launch_row(app.state.db_pool, body)
        calls.append(body)
        if len(calls) == 1:
            raise TimeoutError("native launch acknowledgement lost")
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", lost_once)
    first = client.post(PATH, json=BODY, headers=HEADERS)
    assert first.status_code == 502, first.text
    snapshot = _snapshot(db_conn)
    retry = client.post(PATH, json=BODY, headers=HEADERS)
    assert retry.status_code == 201, retry.text
    assert len(calls) == 2
    assert all(call.launch_attempt_id == snapshot[2] for call in calls)
    assert all(call.agent_id == retry.json()["id"] for call in calls)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert _snapshot(db_conn) == snapshot
