"""IM directory pagination and direct detail lookup use the gateway contract."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from services.im_bridge.gateway_client import GatewayClient


def test_directory_page_passes_scope_search_and_cursor() -> None:
    page = {
        "agents": [{"agent_id": 405, "label": "Target", "status": "running"}],
        "next_cursor": 405,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents"
        assert dict(request.url.params) == {
            "scope": "live",
            "query": "Target",
            "before_id": "500",
            "limit": "100",
        }
        assert request.headers["cookie"] == "session=test"
        return httpx.Response(200, json=page)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="http://gateway", transport=httpx.MockTransport(handler)
        ) as http:
            client = GatewayClient()
            client._client = http
            client._cookie = "session=test"
            assert await client.list_agents(scope="live", query="Target", before_id=500) == page

    asyncio.run(scenario())


@pytest.mark.parametrize("status_code", [200, 404, 503])
def test_detail_lookup_distinguishes_missing_agent_and_gateway_failure(status_code: int) -> None:
    detail = {"agent_id": 405, "label": "Target", "status": "running", "pid": 42}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/405"
        assert not request.url.query
        return httpx.Response(status_code, json=detail)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="http://gateway", transport=httpx.MockTransport(handler)
        ) as http:
            client = GatewayClient()
            client._client = http
            if status_code == 503:
                with pytest.raises(RuntimeError, match="get agent 405 failed: HTTP 503"):
                    await client.get_agent(405)
            else:
                assert await client.get_agent(405) == (detail if status_code == 200 else None)

    asyncio.run(scenario())


# ── SSE stream: Unicode line separators that are legal raw in JSON ───────
#
# U+0085 / U+2028 / U+2029 may appear unescaped inside a JSON string and
# the gateway emits them raw, so the frame decoder must never split a data
# line there - a split truncates the payload and the frame is dropped.


@pytest.mark.parametrize("ch", ("\u0085", "\u2028", "\u2029"), ids=("U+0085", "U+2028", "U+2029"))
def test_stream_events_keeps_unicode_line_separators(ch: str) -> None:
    """One frame, one data line; the payload survives the stream decode."""
    payload = {"role": "timeline_snapshot", "content": f"a{ch}b"}
    frame = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/7/events/stream"
        return httpx.Response(
            200,
            content=frame.encode(),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            base_url="http://gateway", transport=httpx.MockTransport(handler)
        ) as http:
            client = GatewayClient()
            client._client = http
            return [event async for event in client.stream_events(7)]

    assert asyncio.run(scenario()) == [payload]
