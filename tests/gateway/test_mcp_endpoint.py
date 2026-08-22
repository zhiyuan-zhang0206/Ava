"""Contract tests for the gateway /mcp endpoint (design task #1212 step 1).

The endpoint is flag-gated (`settings.gateway.mcp_endpoint_enabled`, default
off) and additive: off, /mcp answers 404 and the rest of the gateway is
unchanged. On, it speaks stateless Streamable HTTP (2026-07-28 revision) —
every POST carries one JSON-RPC message, responses come back as an SSE stream
(the transport default; the client must accept application/json AND
text/event-stream).

Tests run the real app via TestClient (its lifespan builds the MCP session
manager and enters `manager.run()`), with the ops-routing autouse fixtures
from tests/gateway/conftest.py standing in for the local runner.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import config, telemetry
from shared.cluster_auth import bearer_header

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture
_ACCEPT = "application/json, text/event-stream"


@pytest.fixture(autouse=True)
def _enable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.gateway, "mcp_endpoint_enabled", True)


def _sse_messages(body: str) -> list[dict[str, Any]]:
    """Every JSON-RPC message out of an SSE response body, in order."""
    out: list[dict[str, Any]] = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].strip() for line in event.splitlines() if line.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def _post(
    client: TestClient, payload: dict[str, Any], *, auth: bool = False
) -> list[dict[str, Any]]:
    headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
    if auth:
        headers.update(bearer_header(_SECRET))
    resp = client.post("/mcp", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return _sse_messages(resp.text)


def _initialize(client: TestClient) -> list[dict[str, Any]]:
    return _post(
        client,
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


def _tool_call(
    client: TestClient, name: str, args: dict[str, Any], req_id: int = 1
) -> dict[str, Any]:
    messages = _post(
        client,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert len(messages) == 1
    return messages[0]


def _tool_result(message: dict[str, Any]) -> Any:
    """The tool's return value out of a tools/call response.

    MCPServer wraps dict/list return values as `structuredContent.result`
    (the same wire shape the existing stdio `ava mcp serve` produces for the
    same tool signatures); an error result carries the message in the first
    text block.
    """
    if message["result"].get("isError"):
        return message["result"]["content"][0]["text"]
    structured: dict[str, Any] = message["result"]["structuredContent"]
    # A list return value is wrapped as {"result": [...]} (structuredContent
    # must be a JSON object); dict returns pass through unwrapped.
    if list(structured) == ["result"]:
        return structured["result"]
    return structured


# ── flag off: additive surface, nothing changes ──────────────────────────


def test_disabled_endpoint_answers_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.gateway, "mcp_endpoint_enabled", False)
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": _ACCEPT, "Content-Type": "application/json"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "mcp endpoint is disabled"


# ── handshake + tool surface ─────────────────────────────────────────────


def test_initialize_negotiates_and_lists_seven_tools() -> None:
    with TestClient(app) as client:
        init = _initialize(client)
        assert init[0]["result"]["protocolVersion"]
        messages = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"] for t in messages[0]["result"]["tools"]}
    assert tools == {
        "list_agents",
        "get_agent",
        "spawn_agent",
        "send_message",
        "get_messages",
        "terminate_agent",
        "cluster_status",
    }


def test_list_agents_returns_compact_rows() -> None:
    with TestClient(app) as client:
        result = _tool_call(client, "list_agents", {})
    rows = _tool_result(result)
    assert rows == []  # empty fleet in this test DB
    assert not result["result"].get("isError")


def test_list_agents_rejects_unknown_status() -> None:
    with TestClient(app) as client:
        result = _tool_call(client, "list_agents", {"status": "active"})
    assert result["result"].get("isError") is True
    assert "unknown agent status" in _tool_result(result)


# ── fleet tools against the in-process ops stand-ins ─────────────────────


def test_spawn_get_terminate_round_trip() -> None:
    with TestClient(app) as client:
        payload = _tool_result(_tool_call(client, "spawn_agent", {"prompt": "test goal"}))
        agent_id = int(payload["id"])

        row = _tool_result(_tool_call(client, "get_agent", {"agent_id": agent_id}))
        assert row["agent_id"] == agent_id
        assert row["spawner"] == "mcp"

        status = _tool_result(_tool_call(client, "terminate_agent", {"agent_id": agent_id}))
        assert status["status"] == "enqueued"


def test_get_agent_not_found_is_an_error() -> None:
    with TestClient(app) as client:
        result = _tool_call(client, "get_agent", {"agent_id": 999999})
    assert result["result"].get("isError") is True
    assert "does not exist" in _tool_result(result)


def test_send_message_and_get_messages() -> None:
    with TestClient(app) as client:
        agent_id = int(
            _tool_result(_tool_call(client, "spawn_agent", {"prompt": "test goal"}))["id"]
        )

        sent = _tool_result(
            _tool_call(client, "send_message", {"agent_id": agent_id, "content": "hello"})
        )
        # "idling" is the true delivery-time status of a never-claimed
        # agent — the same value the REST endpoint returns in tests.
        assert sent["status"] in {"queued", "claimed", "idling"}

        payload = _tool_result(
            _tool_call(client, "get_messages", {"agent_id": agent_id, "limit": 10})
        )
        # A never-run test agent has no checkpoint yet — the shape is the
        # contract, the count is honest zero.
        assert isinstance(payload["total"], int)
        assert isinstance(payload["messages"], list)
        assert {m["role"] for m in payload["messages"]} <= {"human", "ai", "system"}


def test_cluster_status_reports_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure-gateway branch: no ops server stand-in exists for status_probe, so
    # the tool takes the local snapshot path (the same branch a gateway-only
    # host serves).
    from gateway.routers import cluster as _cluster_router

    monkeypatch.setattr(_cluster_router, "is_agent_runner", lambda: False)
    with TestClient(app) as client:
        payload = _tool_result(_tool_call(client, "cluster_status", {}))
    assert payload["machine_name"]
    assert isinstance(payload["paused"], bool)


# ── auth: same cluster middleware as every route ─────────────────────────


def test_mcp_requires_auth_when_middleware_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": _ACCEPT, "Content-Type": "application/json"},
        )
    assert resp.status_code == 401


# ── audit: every tools/call lands in the unified event stream ────────────


def test_tool_call_is_audited() -> None:
    with TestClient(app) as client:
        _tool_call(client, "list_agents", {}, req_id=7)
    telemetry.sync()
    from shared.paths import logs_dir

    day = datetime.now(UTC).strftime("%Y%m%d")
    mirror = logs_dir() / f"events-{day}.jsonl"
    lines = mirror.read_text(encoding="utf-8").splitlines()
    hits = [
        json.loads(line)
        for line in lines
        if json.loads(line).get("event_name") == "mcp_tool_call"
        and json.loads(line).get("attributes", {}).get("tool") == "list_agents"
    ]
    assert hits, "no mcp_tool_call event for list_agents reached the mirror"
    assert hits[-1]["attributes"]["outcome"] == "ok"
    assert hits[-1]["category"] == "audit"
    assert hits[-1]["source"] == "mcp"
