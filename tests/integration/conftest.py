"""Shared fixtures for tests/integration/."""

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

# ava.self.AGENT_ID is set by top-level tests/conftest.py (=1), no override here.
from gateway.app import app
from shared.config import settings

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


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all API keys to dummy values so spawn validation passes."""
    from pydantic import SecretStr

    from shared.config import settings as _settings

    for attr in (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "xai_api_key",
    ):
        monkeypatch.setattr(_settings.lm, attr, SecretStr("sk-test"))


@pytest.fixture(autouse=True)
def _local_spawn_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /api/agents` is HTTP-uniform — it always forwards the spawn to a
    runner's ops server over HTTP, even for the local box (localhost). In-process
    there is no live ops daemon, so stand in for it: dispatch `launch_agent_op`
    in-process against the app's pool, exactly what the daemon does on receiving
    the forwarded op (the agent row was already created by the gateway). Routing
    tests that patch `_forward_spawn_to_remote` themselves run after this and win."""
    from gateway.routers import agents as _agents_router
    from ops.ops_lifecycle import launch_agent_op
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
    from shared import machines as _machines
    from shared.machine import machine_name

    async def _in_process_forward(_target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return await launch_agent_op(body, app.state.db_pool)

    monkeypatch.setattr(_agents_router, "_forward_spawn_to_remote", _in_process_forward)

    # `POST /api/agents` reads the target's capability from the registry; the local
    # machine must resolve to agent-runner as `ava start`'s register_self would
    # make it. Other names fall through to the real lookup so the not-registered /
    # gateway-only guard tests still exercise it.
    real_lookup_role = _machines.lookup_role

    def _lookup_role(name: str) -> list[str]:
        if name == machine_name():
            return ["gateway", "agent-runner"]
        return real_lookup_role(name)

    monkeypatch.setattr(_machines, "lookup_role", _lookup_role)

    # Same for the spawn preflight's pause-latch read (is_paused): the local
    # machine is never paused in tests; remote names fall through to the real
    # registry read.
    real_is_paused = _machines.is_paused

    def _is_paused(name: str) -> bool:
        if name == machine_name():
            return False
        return real_is_paused(name)

    monkeypatch.setattr(_machines, "is_paused", _is_paused)


@pytest.fixture
def gateway_client(db_conn: psycopg.Connection) -> Iterator[httpx.Client]:
    """Monkeypatch _gateway_client._client → TestClient transport."""
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    app.state.db_pool = pool

    test_client = TestClient(app)
    transport = _TestClientTransport(test_client)

    import ava._gateway_client as gc

    # the module's `httpx` name is runtime-injected (ava SDK design),
    # so `_client`'s declared type does not resolve statically.
    orig_client = gc._client  # pyright: ignore[reportUnknownMemberType]
    gc._client = httpx.Client(
        transport=transport, base_url="http://testserver", timeout=httpx.Timeout(10.0)
    )

    try:
        yield gc._client  # pyright: ignore[reportUnknownMemberType]
    finally:
        gc._client = orig_client  # pyright: ignore[reportUnknownMemberType]
        pool.close()
