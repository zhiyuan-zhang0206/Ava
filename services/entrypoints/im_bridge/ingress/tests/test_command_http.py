"""Actual gateway transactions distinguish business proof from transport uncertainty."""

import asyncio
import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.tests.test_notices_endpoint import _insert_notice
from services.entrypoints.im_bridge.ingress.tests.conftest import NativeWeixin, message
from services.entrypoints.im_bridge.ingress.types import IngressStatus
from services.entrypoints.im_bridge.types import SpawnDraft
from tests.path_scoped.api_keys import _mock_api_keys as _mock_api_keys
from tests.path_scoped.gateway_tests import _local_spawn_in_process as _local_spawn_in_process


@pytest.mark.parametrize("lost_response", [False, True])
async def test_notice_owner_commits_original_target_once_with_or_without_response(
    native_weixin: NativeWeixin,
    gateway_unit: TestClient,
    db_conn: psycopg.Connection,
    lost_response: bool,
) -> None:
    notice = _insert_notice(db_conn, native_weixin.agent_id, "original", require_response=True)
    later = _insert_notice(db_conn, native_weixin.agent_id, "later", require_response=True)
    bridge = native_weixin.core.notice_bridge
    account = await native_weixin.adapter.outbound_account_id()
    bridge._arm_reply_mode("peer-1", str(native_weixin.agent_id), str(notice), account_id=account)
    calls: list[str] = []
    responses: list[tuple[int, object]] = []

    async def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # A subsequent UI interaction must not redirect the already frozen reply.
        bridge._arm_reply_mode(
            "peer-1", str(native_weixin.agent_id), str(later), account_id=account
        )
        response = await asyncio.to_thread(
            gateway_unit.post,
            request.url.path,
            json=json.loads(request.content),
            headers=dict(request.headers),
        )
        responses.append((response.status_code, response.json()))
        assert response.status_code == 201
        if lost_response:
            raise httpx.ReadError("injected response loss", request=request)
        return httpx.Response(response.status_code, json=response.json())

    async with httpx.AsyncClient(
        base_url="http://isolated-gateway", transport=httpx.MockTransport(gateway)
    ) as http:
        native_weixin.core.gateway._client = http
        payload = message("answer original", "11")
        result = await native_weixin.adapter._handle_message(payload)
        assert result.route.notice_id == notice
        assert result.status == (
            IngressStatus.UNCERTAIN if lost_response else IngressStatus.ACCEPTED
        ), (result, responses)
        assert await native_weixin.adapter._handle_message(payload) == result
    assert calls == [f"/api/agents/{native_weixin.agent_id}/notices/{notice}/resolve"]
    assert bridge.reply_target("peer-1", "new reply", account_id=account) == (
        native_weixin.agent_id,
        later,
    )
    assert db_conn.execute("SELECT reply FROM agent_notices WHERE id=%s", (notice,)).fetchone() == (
        "answer original",
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (native_weixin.agent_id,)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "status,body", [(409, {}), (500, {}), (201, {"inbound_id": True}), (201, {"handled": True})]
)
async def test_notice_error_or_unproven_response_is_not_chat_fallback_or_success(
    native_weixin: NativeWeixin, db_conn: psycopg.Connection, status: int, body: dict[str, object]
) -> None:
    account = await native_weixin.adapter.outbound_account_id()
    native_weixin.core.notice_bridge._arm_reply_mode(
        "peer-1", str(native_weixin.agent_id), "12", account_id=account
    )
    calls: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        base_url="http://isolated-gateway", transport=httpx.MockTransport(gateway)
    ) as http:
        native_weixin.core.gateway._client = http
        payload = message("original retained", "12")
        result = await native_weixin.adapter._handle_message(payload)
        assert result.status == IngressStatus.UNCERTAIN
        assert result.attempt_id is not None and result.route.notice_id == 12
        assert await native_weixin.adapter._handle_message(payload) == result
    assert len(calls) == 1
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


@pytest.mark.parametrize("lost_response", [False, True])
async def test_spawn_actual_birth_response_or_loss_is_never_repeated(
    native_weixin: NativeWeixin,
    gateway_unit: TestClient,
    db_conn: psycopg.Connection,
    lost_response: bool,
) -> None:
    ingress, _binding = await native_weixin.adapter._ensure_ingress()
    from services.entrypoints.im_bridge.types import ChatState

    state = ChatState("weixin", "peer-1")
    state.spawn_draft = SpawnDraft()
    ingress.command_states[("https://provider.example", "bot-id", "peer-1")] = state
    calls: list[tuple[str, str]] = []
    birth_ids: list[int] = []

    async def gateway(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers["Idempotency-Key"]))
        response = await asyncio.to_thread(
            gateway_unit.post,
            request.url.path,
            json=json.loads(request.content),
            headers=dict(request.headers),
        )
        assert response.status_code == 201
        birth_ids.append(response.json()["id"])
        if lost_response:
            raise httpx.ReadError("injected birth response loss", request=request)
        return httpx.Response(201, json=response.json())

    async with httpx.AsyncClient(
        base_url="http://isolated-gateway", transport=httpx.MockTransport(gateway)
    ) as http:
        native_weixin.core.gateway._client = http
        payload = message("spawn:go", "13")
        result = await native_weixin.adapter._handle_message(payload)
        assert result.status == (
            IngressStatus.UNCERTAIN if lost_response else IngressStatus.ACCEPTED
        ), result
        assert await native_weixin.adapter._handle_message(payload) == result
    assert len(calls) == len(birth_ids) == 1
    assert calls[0][0] == "/api/agents"
    assert db_conn.execute(
        "SELECT count(*) FROM agents WHERE id=%s", (birth_ids[0],)
    ).fetchone() == (1,)
    if not lost_response:
        assert result.result == {"agent_id": birth_ids[0]}


@pytest.mark.parametrize("birth_id", [True, "1", 0, -1, None])
async def test_unqualified_birth_id_is_uncertain_not_coerced_to_acceptance(
    native_weixin: NativeWeixin, birth_id: object
) -> None:
    from services.entrypoints.im_bridge.types import ChatState

    ingress, _binding = await native_weixin.adapter._ensure_ingress()
    state = ChatState("weixin", "peer-1", spawn_draft=SpawnDraft())
    ingress.command_states[("https://provider.example", "bot-id", "peer-1")] = state
    calls: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json={"id": birth_id})

    async with httpx.AsyncClient(
        base_url="http://isolated-gateway", transport=httpx.MockTransport(gateway)
    ) as http:
        native_weixin.core.gateway._client = http
        payload = message("spawn:go", "14")
        result = await native_weixin.adapter._handle_message(payload)
        assert result.status == IngressStatus.UNCERTAIN
        assert result.result is None
        assert await native_weixin.adapter._handle_message(payload) == result
    assert calls == ["/api/agents"]
