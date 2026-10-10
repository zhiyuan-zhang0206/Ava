"""Assembled gateway clients, bound only to HTTP and SDK gateway consumers."""

from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.fixtures.unit.identity import _machine_identity


@pytest.fixture
def gateway_unit(db_conn: psycopg.Connection) -> Iterator[TestClient]:
    """The gateway unit (gateway): owns the test DB/config, runs the
    in-process gateway app, role='gateway'.

    Yields a FastAPI TestClient bound to the gateway app. Use for tests that
    exercise gateway endpoints / invariants without hand-patching
    machine_role at each import site. db_conn gives the per-test TRUNCATE. For a
    specific machine_name, also take `set_machine_identity` and call it.

    The cluster-secret auth middleware is disabled by the autouse `_clean_state`
    (auth_middleware_enabled=false) so in-process endpoint tests don't need to
    carry auth headers. Tests that specifically exercise the auth middleware
    re-enable it via monkeypatch.
    """
    from gateway.app import app as _app

    _ = db_conn  # per-test truncate side effect
    with (
        _machine_identity(role="gateway"),
        TestClient(_app, base_url="http://test-gateway") as client,
    ):
        yield client


@pytest.fixture
def sdk_via_gateway(gateway_unit: TestClient) -> Iterator[TestClient]:
    """The gateway unit with the SDK's gateway client pointed at it: `ava.agents.*` calls land
    in the in-process app. Undone at teardown."""
    from ava.gateway_client.transport import use_client

    with use_client(gateway_unit):
        yield gateway_unit
