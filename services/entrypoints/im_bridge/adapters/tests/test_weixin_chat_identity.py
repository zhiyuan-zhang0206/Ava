"""Qualified ordinary Weixin events preserve the Gateway's chat operation identity."""

import asyncio
import json
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from services.entrypoints.im_bridge.adapters.tests.test_weixin_adapter import env as env
from services.entrypoints.im_bridge.adapters.weixin import (
    _ordinary_chat_key,
    load_account,
    save_account,
)
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.ingress.identity import source_chat_key
from services.entrypoints.im_bridge.ingress.types import ProviderSource
from services.entrypoints.im_bridge.tests.slices import im_bridge_config


def set_account(
    *, account_id: str = "bot-id", base_url: str = "https://ilinkai.weixin.qq.com"
) -> None:
    save_account(
        account_id=account_id,
        base_url=base_url,
        user_id="bot-user-id",
        bot_token="test-bot-token",  # noqa: S106 — mock-only credential
    )


async def inbound_key(provider_id: object, text: str = "same") -> str | None:
    account = load_account()
    assert account is not None
    return _ordinary_chat_key(
        base_url=account["base_url"],
        account_id=account["account_id"],
        sender_id="peer-1",
        provider_id=provider_id,
        text=text,
    )


async def test_uint64_identity_is_lossless_and_canonical_across_restart(env: Path) -> None:
    numeric = 18_446_744_073_709_551_615
    keys = [await inbound_key(value) for value in (numeric, str(numeric))]
    assert keys[0] == keys[1]
    assert keys[0] is not None and keys[0].startswith("weixin-chat-v1:")
    assert await inbound_key("00000000000000000123") == await inbound_key(123)
    assert await inbound_key(numeric - 1) != keys[0]


@pytest.mark.parametrize(
    "provider_id",
    [
        None,
        "",
        True,
        False,
        1.0,
        -1,
        0,
        2**64,
        "18446744073709551616",
        " 1",
        "1 ",
        "+1",
        "1.0",
        "1e3",
        "event",
        "\u0661",
        {},
        [],
    ],
)
async def test_unqualified_provider_ids_do_not_claim_stable_chat_identity(
    env: Path, provider_id: object
) -> None:
    assert await inbound_key(provider_id) is None


@pytest.mark.parametrize("text", ["/help", "/unknown", "spawn:preset:test", "notice:read:1:2"])
async def test_commands_and_notice_paths_do_not_receive_ordinary_chat_keys(
    env: Path, text: str
) -> None:
    assert await inbound_key("123", text) is None


async def test_spawn_source_identity_is_account_qualified_not_legacy_peer_hash(env: Path) -> None:
    source = ProviderSource(
        namespace="https://ilinkai.weixin.qq.com",
        account_id="bot-id",
        sender_id="peer-1",
        message_id="123",
    )
    assert source_chat_key(source).startswith("weixin-chat-v1:")
    assert source_chat_key(source.model_copy(update={"account_id": "other"})) != source_chat_key(
        source
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://ilinkai.weixin.qq.com",
        "https://secret@ilinkai.weixin.qq.com",
        "https://user:secret@ilinkai.weixin.qq.com",
        "https://ilinkai.weixin.qq.com?token=secret",
        "https://ilinkai.weixin.qq.com#secret",
        "https://ilinkai.weixin.qq.com:invalid",
        "https:///missing",
    ],
)
async def test_unqualified_endpoint_has_no_strong_key(env: Path, endpoint: str) -> None:
    set_account(base_url=endpoint)
    assert await inbound_key("123") is None


async def test_account_sender_and_endpoint_scope_separate_same_provider_id(env: Path) -> None:
    first = await inbound_key("123")
    set_account(account_id="other-bot")
    assert await inbound_key("123") != first
    set_account(base_url="https://other-provider.example")
    assert await inbound_key("123") != first
    set_account(base_url="https://ILINKAI.WEIXIN.QQ.COM:443/")
    assert await inbound_key("123") == first
    assert (
        _ordinary_chat_key(
            base_url="https://ilinkai.weixin.qq.com",
            account_id="bot-id",
            sender_id="other-peer",
            provider_id="123",
            text="same",
        )
        != first
    )
    set_account(account_id=" ")
    assert await inbound_key("123") is None


async def test_voice_transcript_is_ordinary_chat_with_event_identity(env: Path) -> None:
    from services.entrypoints.im_bridge.adapters.weixin import _extract_text

    text = _extract_text([{"type": 3, "voice_item": {"text": "transcript"}}])
    assert text == "[voice transcript] transcript"
    assert await inbound_key("123", text) == await inbound_key("123")


def create_agent(conn: psycopg.Connection) -> int:
    row = conn.execute("INSERT INTO agents(label) VALUES ('weixin-chat') RETURNING id").fetchone()
    assert row is not None
    conn.execute("INSERT INTO agents_meta(id,status) VALUES (%s,'running')", (row[0],))
    conn.commit()
    return row[0]


async def test_same_event_changed_body_or_selection_does_not_claim_recovery(
    env: Path, gateway_unit: TestClient, db_conn: psycopg.Connection
) -> None:
    first_agent, later_agent = create_agent(db_conn), create_agent(db_conn)
    statuses: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await asyncio.to_thread(
            gateway_unit.post,
            request.url.path,
            json=json.loads(request.content),
            headers=dict(request.headers),
        )
        statuses.append(response.status_code)
        return httpx.Response(response.status_code, json=response.json())

    key = await inbound_key("123")
    async with httpx.AsyncClient(
        base_url="http://test-gateway", transport=httpx.MockTransport(handler)
    ) as transport:
        client = GatewayClient(
            im_bridge_config(im_send_retry_delays=(0.0,)),
            gateway_url="http://test-gateway",
            auth_headers={},
        )
        client._client = transport
        await client.send_message(first_agent, "same", idempotency_key=key)
        with pytest.raises(RuntimeError, match="HTTP 409"):
            await client.send_message(first_agent, "changed", idempotency_key=key)
        with pytest.raises(RuntimeError, match="HTTP 409"):
            await client.send_message(later_agent, "same", idempotency_key=key)
    assert statuses == [201, 409, 409]
    assert db_conn.execute("SELECT agent_id,content FROM inbound_messages").fetchall() == [
        (first_agent, "same")
    ]
