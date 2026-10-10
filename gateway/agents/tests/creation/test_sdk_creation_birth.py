"""The strong SDK recovers an immutable birth across Gateway lifespan restarts."""

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from ava.gateway_client import transport
from base.agents import GatewayUnavailable
from base.config import settings
from gateway.agents import router as agent_router
from gateway.agents.tests.creation.test_guarded_creation import HEADERS, SECRET
from gateway.agents.tests.creation.test_sdk_strong_creation import _response, _sdk
from gateway.app import app
from ops.lifecycle.launch import _validate_launch_row
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from tests.fixtures.gateway_config import gateway_test_client
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process


@pytest.mark.parametrize("rotate", [False, True])
def test_same_sdk_intent_after_lost_response_and_gateway_restart(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    set_machine_identity: Any,
    rotate: bool,
) -> None:
    set_machine_identity(role="agent-runner", name="local-test")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    launches: list[LaunchAgentRequest] = []

    async def accept(_db: object, _target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        # Exercise the runner's real original-attempt/placement fence, rather
        # than treating matching JSON as execution evidence.
        _validate_launch_row(app.state.db_pool, body)
        launches.append(body)
        return SpawnedAgent(id=body.agent_id)

    async def reconcile(*args: object, **kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(agent_router, "forward_spawn_to_remote", accept)
    monkeypatch.setattr("ops.cluster.rpc.dispatch_to_machine", reconcile)
    http = MagicMock()
    lose = True
    target: TestClient

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        nonlocal lose
        response = target.post(path, **kwargs)
        if lose:
            lose = False
            assert response.status_code == 201, response.text
            raise httpx.ReadTimeout("committed birth response lost")
        return _response(response)

    http.post.side_effect = submit
    with transport.use_client(http):
        with gateway_test_client(app, headers={"Authorization": f"Bearer {SECRET}"}) as target:
            with pytest.raises(GatewayUnavailable):
                _sdk()
            assert http.post.call_count == 1
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
        with gateway_test_client(app, headers={"Authorization": f"Bearer {SECRET}"}) as target:
            assert _sdk() == agent_id
    assert http.post.call_count == 2
    assert http.post.call_args_list[0] == http.post.call_args_list[1]
    assert len(launches) == (1 if rotate else 2)
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
