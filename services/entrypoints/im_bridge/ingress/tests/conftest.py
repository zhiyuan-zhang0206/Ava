"""Actual core, registered adapter, native selection and isolated provider transports."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database, create_agent
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter, save_account
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore
from services.entrypoints.im_bridge.tests.slices import im_bridge_config
from services.entrypoints.im_bridge.tests.task_scope import owned_tasks
from tests.fixtures.unit.sdk import sdk_environment as sdk_environment
from tests.fixtures.unit.sdk import sdk_identity as sdk_identity
from tests.fixtures.unit.sdk import sdk_metering as sdk_metering


@dataclass
class NativeWeixin:
    core: IMBridgeCore
    adapter: WeixinAdapter
    pool: ConnectionPool
    agent_id: int
    gateway_requests: list[httpx.Request]
    provider_requests: list[httpx.Request]
    provider_responses: list[dict[str, object]]


def message(text: str, message_id: object = "1", *, sender: str = "peer-1") -> dict[str, object]:
    return {
        "from_user_id": sender,
        "to_user_id": "bot-id",
        "message_id": message_id,
        "message_type": 1,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


@pytest.fixture
async def native_weixin(
    database: Database,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[NativeWeixin]:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    save_account(
        account_id="bot-id",
        bot_token="isolated-test-token",  # noqa: S106
        user_id="owner",
        base_url="https://provider.example",
    )
    agent_id = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta(id,status) VALUES (%s,'running')", (agent_id,))
    db_conn.commit()
    gateway_requests: list[httpx.Request] = []
    provider_requests: list[httpx.Request] = []
    provider_responses: list[dict[str, object]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        gateway_requests.append(request)
        if request.method == "GET" and request.url.path == f"/api/agents/{agent_id}":
            return httpx.Response(
                200, json={"agent_id": agent_id, "label": "Selected", "status": "running"}
            )
        if request.method == "GET" and request.url.path.endswith("/timeline"):
            return httpx.Response(200, json={"items": []})
        if request.method == "GET" and request.url.path == "/api/presets":
            return httpx.Response(200, json=[])
        raise AssertionError("unexpected gateway call in native Weixin fixture")

    def provider(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        if request.url.path.endswith("getupdates"):
            if provider_responses:
                return httpx.Response(200, json=provider_responses.pop(0))
            return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": ""})
        return httpx.Response(200, json={"ret": 0})

    config = im_bridge_config(im_send_retry_delays=(0.0,))
    with database.pool(min_size=1, max_size=3) as pool:
        async with (
            owned_tasks() as tasks,
            httpx.AsyncClient(
                base_url="http://isolated-gateway", transport=httpx.MockTransport(gateway)
            ) as gateway_http,
            httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_http,
        ):
            client = GatewayClient(config, gateway_url="http://isolated-gateway", auth_headers={})
            client._client = gateway_http
            core = IMBridgeCore(config, client, db_pool=pool, tasks=tasks)
            adapter = WeixinAdapter(core, client=provider_http)
            core.register(adapter)

            def no_subscription(*_args: object, **_kwargs: object) -> None:
                pass

            monkeypatch.setattr(core, "_ensure_subscription", no_subscription)
            monkeypatch.setattr(core, "_start_typing", no_subscription)
            store = WeixinIngressStore(pool)
            binding = store.initialize("https://provider.example", "bot-id", None)
            binding = store.begin_cutover(binding, expected_cursor="")
            store.checkpoint(binding, "", "", provider_empty=True)
            state = core._get_or_create_state("weixin", "peer-1")
            await core._cmd_switch(state, str(agent_id), replay_id="fixture-explicit-selection")
            gateway_requests.clear()
            yield NativeWeixin(
                core,
                adapter,
                pool,
                agent_id,
                gateway_requests,
                provider_requests,
                provider_responses,
            )
