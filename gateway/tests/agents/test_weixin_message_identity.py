import asyncio
import json
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.tests.adapters.test_weixin_adapter import env as env
from services.entrypoints.im_bridge.tests.adapters.test_weixin_chat_identity import inbound_key
from services.entrypoints.im_bridge.tests.slices import im_bridge_config


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
