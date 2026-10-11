"""Guarded routing must reject legacy servers and unverified creation identities."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.config import settings
from gateway.agents import router as agent_router
from gateway.agents.creation import scoped_creation_key
from gateway.app import app
from gateway.http.auth.request_principal import AuthPrincipal, principal_key
from gateway.tests.extensions.test_mcp_endpoint import _tool_call, _tool_result
from ops.agents.creation_identity import creation_request_hash
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent
from tests.fixtures.gateway_config import create_mcp_client, gateway_test_client
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process

PATH = "/api/keyed/v1/agents"
SECRET = "guarded-test-secret"  # noqa: S105 -- isolated auth fixture
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}


@pytest.fixture
def client(
    gateway_data_plane: None,
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity: Any,
    *,
    machine_identity: MachineIdentity,
) -> Iterator[TestClient]:
    set_machine_identity(role="agent-runner", name="local-test")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)

    async def accept(db: object, target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", accept)
    with gateway_test_client(
        app, headers={"Authorization": f"Bearer {SECRET}"}, machine=machine_identity
    ) as value:
        yield value


def _count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM agents").fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "", "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x" * 129, "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "intent"},
        {"Idempotency-Key": "intent", "Idempotency-Scope": "legacy"},
    ],
)
def test_invalid_admission_has_no_birth(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    assert client.post(PATH, json={}, headers=headers).status_code == 422
    assert _count(db_conn) == 0


def test_fork_without_source_checkpoint_has_no_birth(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    parent = client.post(PATH, json={}, headers={**HEADERS, "Idempotency-Key": "parent"})
    assert parent.status_code == 201, parent.text
    assert (
        client.post(PATH, json={"fork_from": parent.json()["id"]}, headers=HEADERS).status_code
        == 409
    )
    assert _count(db_conn) == 1


def test_lost_response_and_concurrency_replay_one_birth(
    client: TestClient,
    db_conn: psycopg.Connection,
) -> None:
    body = {"machine": "local-test", "prompt": "one goal", "prompt_source": "user"}
    accepted = client.post(PATH, json=body, headers=HEADERS)
    assert accepted.status_code == 201, accepted.text
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(client.post, PATH, json=body, headers=HEADERS) for _ in range(3)]
        responses = [future.result() for future in futures]
    assert all(response.status_code == 201 for response in responses)
    assert all(response.json()["id"] == accepted.json()["id"] for response in responses)
    assert _count(db_conn) == 1
    key = principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "intent")
    row = db_conn.execute("SELECT creation_key FROM agent_creation_snapshots").fetchone()
    assert row is not None and row[0] == key
    assert client.post(PATH, json={**body, "prompt": "changed"}, headers=HEADERS).status_code == 409
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_revoked_credential_cannot_replay_guarded_receipt(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.post(PATH, json={"machine": "local-test"}, headers=HEADERS).status_code == 201
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated-guarded-secret")
    assert client.post(PATH, json={}, headers=HEADERS).status_code == 401
    assert _count(db_conn) == 1


def test_preexisting_mcp_canonical_hash_replays_original_agent(
    gateway_data_plane: None,
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.gateway, "mcp_endpoint_enabled", True)
    # The MCP manager is built by lifespan, so re-enter with the flag enabled.
    with gateway_test_client(
        app, headers={"Authorization": f"Bearer {SECRET}"}, machine=machine_identity
    ) as mcp:
        credentials = create_mcp_client(mcp, "prior", "write").json()
        token = credentials["token"]
        original = _tool_result(
            _tool_call(
                mcp,
                token,
                "spawn_agent_guarded_v1",
                {"prompt": "prior goal", "machine": "local-test", "idempotency_key": "prior-key"},
            )
        )
        canonical = principal_key(
            AuthPrincipal("mcp_client", str(credentials["id"])),
            "POST",
            "/mcp/tools/spawn_agent_guarded_v1",
            "prior-key",
        )
        body = SpawnAgentRequest(
            prompt="prior goal", prompt_source="user", spawner="mcp", machine="local-test"
        )
        fingerprint = creation_request_hash(body.model_dump(mode="json"))
        recorded = db_conn.execute(
            "SELECT creation_key, request_hash FROM agent_creation_snapshots WHERE agent_id=%s",
            (original["id"],),
        ).fetchone()
        assert recorded == (canonical, fingerprint)
        replay = _tool_result(
            _tool_call(
                mcp,
                token,
                "spawn_agent_guarded_v1",
                {"prompt": "prior goal", "machine": "local-test", "idempotency_key": "prior-key"},
            )
        )
        assert replay["id"] == original["id"]
        assert _count(db_conn) == 1


def test_scoped_creation_key_binds_the_principal_and_the_operation_path() -> None:
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/keyed/v1/agents",
            "headers": [(b"idempotency-scope", b"principal-v1")],
        }
    )
    principal = AuthPrincipal("cluster", "administrator")
    request.state.auth_principal = principal
    assert scoped_creation_key(request, "prior", operation_path=PATH) == principal_key(
        principal, "POST", PATH, "prior"
    )
