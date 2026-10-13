"""The strong SDK recovers an immutable birth across Gateway lifespan restarts."""

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from ava.gateway_client import transport
from base.api_contracts.agent_contract import GatewayUnavailable
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.host.net.tests.retry_waits import retry_waits as retry_waits
from gateway.agents import creation as agent_creation
from gateway.app import app
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS, SECRET
from gateway.tests.lifecycle.creation.test_sdk_strong_creation import _response, sdk
from ops.cluster import rpc
from ops.lifecycle.launch import _validate_launch_row
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from tests.fixtures.gateway_config import gateway_test_client
from tests.fixtures.unit.identity import machine_identity as machine_identity
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process


@pytest.mark.usefixtures("retry_waits")
@pytest.mark.parametrize("rotate", [False, True])
def test_same_sdk_intent_after_lost_response_and_gateway_restart(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity: Any,
    rotate: bool,
    *,
    machine_identity: MachineIdentity,
) -> None:
    set_machine_identity(role="agent-runner", name="local-test")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    launches: list[LaunchAgentRequest] = []

    async def accept(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        # Exercise the runner's real original-attempt/placement fence, rather
        # than treating matching JSON as execution evidence.
        _validate_launch_row(app.state.db_pool, body, local_machine=machine_identity.name())
        launches.append(body)
        return SpawnedAgent(id=body.agent_id)

    async def reconcile(*args: object, **kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(agent_creation, "forward_spawn_to_remote", accept)
    monkeypatch.setattr(rpc, "dispatch_to_machine", reconcile)
    http = MagicMock()
    lose = True
    target: TestClient

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        nonlocal lose
        response = target.post(path, **kwargs)
        if lose:
            assert response.status_code == 201, response.text
            raise httpx.ReadTimeout("committed birth response lost")
        return _response(response)

    http.post.side_effect = submit
    with transport.use_client(http):
        with gateway_test_client(
            app, headers={"Authorization": f"Bearer {SECRET}"}, machine=machine_identity
        ) as target:
            with pytest.raises(GatewayUnavailable):
                sdk()
            lost_attempts = http.post.call_count
            assert lost_attempts > 1
            lose = False
            row = db_conn.execute(
                "SELECT agent_id, launch_attempt_id FROM agent_creation_snapshots"
            ).fetchone()
            assert row is not None
            agent_id, original = row
            assert launches[0].launch_attempt_id == original
            if rotate:
                retry = target.post(
                    f"/api/keyed/v1/agents/{agent_id}/retry-launch",
                    json={"expected_prior_attempt_id": str(original)},
                    headers={**HEADERS, "Idempotency-Key": "later-retry"},
                )
                assert retry.status_code == 200, retry.text
                assert retry.json()["launch_attempt_id"] != str(original)
        # A new lifespan owns fresh pools and no prior acceptance cache.
        with gateway_test_client(
            app, headers={"Authorization": f"Bearer {SECRET}"}, machine=machine_identity
        ) as target:
            assert sdk() == agent_id
    assert http.post.call_count == lost_attempts + 1
    assert all(call == http.post.call_args_list[0] for call in http.post.call_args_list)
    # Each replay before the rotation re-offers the original launch; a replay
    # after it finds the attempt superseded and offers nothing.
    assert len(launches) == (lost_attempts if rotate else lost_attempts + 1)
    assert all(launch.launch_attempt_id == original for launch in launches)
    _assert_one_birth(db_conn, original)


def _assert_one_birth(conn: psycopg.Connection, original: UUID) -> None:
    assert conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT launch_attempt_id FROM agent_creation_snapshots").fetchone() == (
        original,
    )
