"""Shared fixtures for the integration tests (registered by `tests/fixtures/path_scopes.py`)."""

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from ava.gateway_client.transport import use_client
from base.cluster.machine import MachineIdentity

# The provider-key mock and the in-process spawn stand-in are defined once (the ava and
# gateway tests take them too); imported here so they register for this module's paths.
from base.config.tests.api_keys import mock_api_keys as mock_api_keys

# ava.self.AGENT_ID is set by tests/fixtures/env_bootstrap.py (=1), no override here.
from gateway.app import app
from tests.fixtures.gateway_config import gateway_test_client
from tests.path_scoped.gateway_tests import gateway_config_owner as gateway_config_owner
from tests.path_scoped.gateway_tests import local_spawn_in_process as local_spawn_in_process

# DB/Redis env (AVA_DB_URL / AVA_REDIS_URL) for spawned-subprocess inheritance is
# synced by the `provisioned_db` / `provisioned_redis` session fixtures when a
# test pulls them (via db_conn / provisioned_redis) — no import-time capture here,
# which would otherwise freeze the unreachable sentinel before provisioning runs.


class _TestClientTransport(httpx.BaseTransport):
    """Forward httpx requests to FastAPI TestClient, in-process communication."""

    def __init__(self, test_client: TestClient):
        self._tc = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        path = url.replace("http://testserver", "")
        headers = dict(request.headers)
        content = request.content
        if method == "POST":
            r = self._tc.post(path, content=content, headers=headers)
        elif method == "GET":
            r = self._tc.get(path, headers=headers)
        else:
            r = self._tc.request(method, path, content=content, headers=headers)
        return httpx.Response(
            status_code=r.status_code,
            headers=dict(r.headers),
            content=r.content,
            request=request,
        )


@pytest.fixture
def gateway_client(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    gateway_config_owner: None,
    machine_identity: MachineIdentity,
) -> Iterator[httpx.Client]:
    """Point the SDK's gateway client at the in-process gateway app (a TestClient transport)."""
    with gateway_test_client(app, machine=machine_identity) as test_client:
        transport = _TestClientTransport(test_client)
        with (
            httpx.Client(
                transport=transport, base_url="http://testserver", timeout=httpx.Timeout(10.0)
            ) as client,
            use_client(client),
        ):
            yield client
