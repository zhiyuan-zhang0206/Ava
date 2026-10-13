"""Assembled gateway clients, bound only to HTTP and SDK gateway consumers."""

from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.config.tests.configuration import snapshot_process_config


@pytest.fixture
def gateway_unit(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
) -> Iterator[TestClient]:
    """The gateway unit (gateway): owns the test DB/config, runs the
    in-process gateway app, role='gateway'.

    Yields a FastAPI TestClient bound to the gateway app. Use for tests that
    exercise gateway endpoints with the explicit machine owner borrowed by the
    actual process root. db_conn gives the per-test TRUNCATE. For a specific
    name, also take `set_machine_identity` and configure the same fixture owner.

    The client is authenticated: the autouse `_clean_state` gives every
    `TestClient` over the gateway app the suite's cluster secret as its bearer
    (`gateway.tests.agents.gateway_auth`), so endpoint tests carry no auth headers.
    Tests that exercise rejection drop it with `without_credentials(client)`.
    """
    from gateway import app as gateway_app
    from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess

    config = snapshot_process_config()
    config.boot()
    machine_identity.set(role="gateway")
    process = GatewayProcess(config=config, image=LOADED_IMAGE, machine=machine_identity)
    monkeypatch.setattr(gateway_app.app.state, "gateway_process_input", process, raising=False)
    _ = db_conn  # per-test truncate side effect
    with (
        process.lifetime(),
        TestClient(gateway_app.app, base_url="http://test-gateway") as client,
    ):
        yield client


@pytest.fixture
def sdk_via_gateway(gateway_unit: TestClient) -> Iterator[TestClient]:
    """The gateway unit with the SDK's gateway client pointed at it: `ava.agents.*` calls land
    in the in-process app. Undone at teardown."""
    from ava.gateway_client.transport import use_client

    with use_client(gateway_unit):
        yield gateway_unit
