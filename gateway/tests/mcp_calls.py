"""JSON-RPC helpers for the gateway /mcp endpoint tests: SSE decoding, client tokens, tool calls."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from base import config
from tests.fixtures.gateway_config import create_mcp_client

ACCEPT = "application/json, text/event-stream"


def sse_messages(body: str) -> list[dict[str, Any]]:
    """Every JSON-RPC message out of an SSE response body, in order.

    Lines split on "\n" only - never str.splitlines(): that also breaks on
    U+0085 / U+2028 / U+2029, which are legal unescaped inside a JSON
    string (the /mcp writer emits them raw), and a split there truncates
    the JSON literal being decoded.
    """
    out: list[dict[str, Any]] = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].strip() for line in event.split("\n") if line.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def create_token(client: TestClient, *, name: str = "test", scope: str = "write") -> str:
    response = create_mcp_client(client, name, scope)
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def post_message(client: TestClient, token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    headers = {
        "Accept": ACCEPT,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    resp = client.post("/mcp", json=payload, headers=headers)
    assert resp.status_code == 200, f"history={resp.history!r} body={resp.text}"
    return sse_messages(resp.text)


def initialize(client: TestClient, token: str) -> list[dict[str, Any]]:
    return post_message(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )


def tool_call(
    client: TestClient,
    token: str,
    name: str,
    args: dict[str, Any],
    req_id: int = 1,
) -> dict[str, Any]:
    messages = post_message(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert len(messages) == 1
    return messages[0]


def tool_result(message: dict[str, Any]) -> Any:
    """The tool's return value out of a tools/call response.

    MCPServer wraps dict/list return values as `structuredContent.result`
    (the wire shape for dict/list tool signatures); an error result carries the
    message in the first text block.
    """
    if message["result"].get("isError"):
        return message["result"]["content"][0]["text"]
    structured: dict[str, Any] = message["result"]["structuredContent"]
    # A list return value is wrapped as {"result": [...]} (structuredContent
    # must be a JSON object); dict returns pass through unwrapped.
    if list(structured) == ["result"]:
        return structured["result"]
    return structured


@pytest.fixture(autouse=True)
def enable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The /mcp endpoint is flag-gated off by default; the tests that import this run it on."""
    monkeypatch.setattr(config.settings.gateway, "mcp_endpoint_enabled", True)
