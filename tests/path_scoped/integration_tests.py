"""Shared fixtures for the integration tests (registered by `tests/fixtures/path_scopes.py`)."""

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from ava.gateway_client.transport import use_client
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus

# ava.self.AGENT_ID is set by tests/fixtures/env_bootstrap.py (=1), no override here.
from gateway.app import app

# The provider-key mock and the in-process spawn stand-in are defined once (the ava and
# gateway tests take them too); imported here so they register for this module's paths.
from tests.path_scoped.api_keys import _mock_api_keys as _mock_api_keys
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process

# DB/Redis env (AVA_DB_URL / AVA_REDIS_URL) for spawned-subprocess inheritance is
# synced by the `_provisioned_db` / `_provisioned_redis` session fixtures when a
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
def gateway_client(db_conn: psycopg.Connection) -> Iterator[httpx.Client]:
    """Point the SDK's gateway client at the in-process gateway app (a TestClient transport)."""
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    app.state.db = Database.from_settings()
    app.state.bus = EventBus.from_settings()
    app.state.db_pool = pool

    test_client = TestClient(app)
    transport = _TestClientTransport(test_client)

    client = httpx.Client(
        transport=transport, base_url="http://testserver", timeout=httpx.Timeout(10.0)
    )
    try:
        with use_client(client):
            yield client
    finally:
        pool.close()
