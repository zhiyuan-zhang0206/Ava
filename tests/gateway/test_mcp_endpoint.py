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

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
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


def _create_token(client: TestClient, *, name: str = "test", scope: str = "write") -> str:
    response = client.post("/api/mcp/clients", json={"name": name, "scope": scope})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _post(client: TestClient, token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    headers = {
        "Accept": _ACCEPT,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    resp = client.post("/mcp", json=payload, headers=headers)
    assert resp.status_code == 200, f"history={resp.history!r} body={resp.text}"
    return _sse_messages(resp.text)


def _initialize(client: TestClient, token: str) -> list[dict[str, Any]]:
    return _post(
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


def _tool_call(
    client: TestClient,
    token: str,
    name: str,
    args: dict[str, Any],
    req_id: int = 1,
) -> dict[str, Any]:
    messages = _post(
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


def _audit_hits(tool: str, client_name: str) -> list[dict[str, Any]]:
    telemetry.sync()
    from shared.paths import logs_dir

    day = datetime.now(UTC).strftime("%Y%m%d")
    mirror = logs_dir() / f"events-{day}.jsonl"
    lines = mirror.read_text(encoding="utf-8").splitlines()
    return [
        json.loads(line)
        for line in lines
        if json.loads(line).get("event_name") == "mcp_tool_call"
        and json.loads(line).get("attributes", {}).get("tool") == tool
        and json.loads(line).get("attributes", {}).get("client_name") == client_name
    ]


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
    assert resp.json()["code"] == "mcp_endpoint_disabled"


# ── handshake + tool surface ─────────────────────────────────────────────


def test_initialize_negotiates_and_lists_seven_tools() -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        init = _initialize(client, token)
        assert init[0]["result"]["protocolVersion"]
        messages = _post(client, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
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


def test_list_agents_uses_summary_projection_and_returns_compact_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import mcp_endpoint
    from shared.agent_snapshot import AgentListSummary

    seen: dict[str, str] = {}
    snapshot = AgentListSummary.model_validate(
        {
            "agent_id": 1,
            "spawner": "mcp",
            "fork_source_agent_id": None,
            "status": "idling",
            "pid": 123,
            "spawned_at": "2026-09-01T00:00:00Z",
            "started_at": "2026-09-01T00:00:01Z",
            "last_active_at": "2026-09-01T00:00:02Z",
            "last_inbound_at": "2026-09-01T00:00:02Z",
            "label": "MCP worker",
            "machine": "test-machine",
            "supports_vision": True,
            "liveness_state": "online",
            "notices_awaiting_response": [],
            "unread_notice_count": 0,
            "heartbeat_paused_until": None,
        }
    )

    def fake_select_all(_conn: object, *, fields: str) -> list[AgentListSummary]:
        seen["fields"] = fields
        return [snapshot]

    monkeypatch.setattr(mcp_endpoint.agent_snapshot, "select_all", fake_select_all)
    with TestClient(app) as client:
        token = _create_token(client)
        result = _tool_call(client, token, "list_agents", {})
    rows = _tool_result(result)
    assert seen == {"fields": "summary"}
    assert rows == [
        {
            "agent_id": 1,
            "status": "idling",
            "label": "MCP worker",
            "machine": "test-machine",
            "spawner": "mcp",
            "last_active_at": "2026-09-01T00:00:02+00:00",
        }
    ]
    assert not result["result"].get("isError")


def test_list_agents_rejects_unknown_status() -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        result = _tool_call(client, token, "list_agents", {"status": "active"})
    assert result["result"].get("isError") is True
    assert "unknown agent status" in _tool_result(result)


# ── fleet tools against the in-process ops stand-ins ─────────────────────


def test_spawn_get_terminate_round_trip(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        payload = _tool_result(_tool_call(client, token, "spawn_agent", {"prompt": "test goal"}))
        agent_id = int(payload["id"])

        row = _tool_result(_tool_call(client, token, "get_agent", {"agent_id": agent_id}))
        assert row["agent_id"] == agent_id
        assert row["spawner"] == "mcp"

        status = _tool_result(
            _tool_call(
                client,
                token,
                "terminate_agent",
                {"agent_id": agent_id, "message": "retain this result"},
            )
        )
        assert status["status"] == "enqueued"
    assert db_conn.execute(
        "SELECT content,kind,source FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()[-2:] == [
        ("retain this result", "chat", "user"),
        ("", "terminate", "user"),
    ]


def test_get_agent_not_found_is_an_error() -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        result = _tool_call(client, token, "get_agent", {"agent_id": 999999})
    assert result["result"].get("isError") is True
    assert "does not exist" in _tool_result(result)


def test_send_message_and_get_messages() -> None:
    with TestClient(app) as client:
        token = _create_token(client)
        agent_id = int(
            _tool_result(_tool_call(client, token, "spawn_agent", {"prompt": "test goal"}))["id"]
        )

        sent = _tool_result(
            _tool_call(
                client,
                token,
                "send_message",
                {"agent_id": agent_id, "content": "hello"},
            )
        )
        # "idling" is the true delivery-time status of a never-claimed
        # agent — the same value the REST endpoint returns in tests.
        assert sent["status"] in {"queued", "claimed", "idling"}

        payload = _tool_result(
            _tool_call(
                client,
                token,
                "get_messages",
                {"agent_id": agent_id, "limit": 10},
            )
        )
        # A never-run test agent has no checkpoint yet — the shape is the
        # contract, the count is honest zero.
        assert isinstance(payload["total"], int)
        assert isinstance(payload["messages"], list)
        assert {m["role"] for m in payload["messages"]} <= {"human", "ai", "system"}


def test_token_clients_same_key_are_isolated_and_body_reuse_conflicts() -> None:
    with TestClient(app) as client:
        first = _create_token(client, name="idem-first")
        second = _create_token(client, name="idem-second")
        agent_id = int(
            _tool_result(_tool_call(client, first, "spawn_agent", {"prompt": "goal"}))["id"]
        )
        args = {"agent_id": agent_id, "content": "same", "idempotency_key": "same-key"}
        one = _tool_result(_tool_call(client, first, "send_message", args))
        repeat = _tool_result(_tool_call(client, first, "send_message", args))
        two = _tool_result(_tool_call(client, second, "send_message", args))
        assert one["inbound_id"] == repeat["inbound_id"]
        assert one["inbound_id"] != two["inbound_id"]
        conflict = _tool_call(client, first, "send_message", args | {"content": "different"})
        assert conflict["result"].get("isError") is True
        assert "different message" in _tool_result(conflict)


def test_revoked_client_cannot_replay_prior_key() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/mcp/clients", json={"name": "idem-revoked", "scope": "write"}
        ).json()
        token = created["token"]
        agent_id = int(
            _tool_result(_tool_call(client, token, "spawn_agent", {"prompt": "goal"}))["id"]
        )
        args = {"agent_id": agent_id, "content": "same", "idempotency_key": "prior-key"}
        _tool_call(client, token, "send_message", args)
        client.post(f"/api/mcp/clients/{created['id']}/revoke").raise_for_status()
        response = client.post(
            "/mcp",
            headers={"Accept": _ACCEPT, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "send_message", "arguments": args},
            },
        )
        assert response.status_code == 401


def test_read_scope_cannot_call_write_tools() -> None:
    with TestClient(app) as client:
        token = _create_token(client, name="read-client", scope="read")
        denied = _tool_call(client, token, "spawn_agent", {"prompt": "must not run"})
        agents = _tool_result(_tool_call(client, token, "list_agents", {}))

    assert denied["result"].get("isError") is True
    assert "requires write scope" in _tool_result(denied)
    assert agents == []


def test_scope_denial_is_audited_as_an_error_without_raw_args() -> None:
    prompt = "blocked sensitive prompt"
    with TestClient(app) as client:
        token = _create_token(client, name="audit-read-client", scope="read")
        _tool_call(client, token, "spawn_agent", {"prompt": prompt})

    hits = _audit_hits("spawn_agent", "audit-read-client")
    assert hits, "no mcp_tool_call event for the denied spawn reached the mirror"
    attributes = hits[-1]["attributes"]
    assert attributes["outcome"] == "error"
    assert attributes["error"] == "tool call returned an error"
    assert prompt not in json.dumps(attributes, ensure_ascii=False)


def test_tool_error_does_not_reintroduce_raw_args_into_audit() -> None:
    status = "private-invalid-status"
    with TestClient(app) as client:
        token = _create_token(client, name="audit-error-client", scope="read")
        result = _tool_call(client, token, "list_agents", {"status": status})

    assert status in _tool_result(result)
    hits = _audit_hits("list_agents", "audit-error-client")
    assert hits, "no mcp_tool_call event for invalid status reached the mirror"
    attributes = hits[-1]["attributes"]
    assert attributes["outcome"] == "error"
    assert status not in json.dumps(attributes, ensure_ascii=False)


def test_cluster_status_reports_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure-gateway branch: no ops server stand-in exists for status_probe, so
    # the tool takes the local snapshot path (the same branch a gateway-only
    # host serves).
    from gateway.routers import cluster as _cluster_router

    monkeypatch.setattr(_cluster_router, "is_agent_runner", lambda: False)
    with TestClient(app) as client:
        token = _create_token(client)
        payload = _tool_result(_tool_call(client, token, "cluster_status", {}))
    assert payload["machine_name"]
    assert isinstance(payload["paused"], bool)


# ── auth: dedicated per-client credentials ───────────────────────────────


@pytest.mark.parametrize("authorization", [None, "Bearer not-a-client-token"])
def test_mcp_rejects_missing_or_bad_client_token(
    monkeypatch: pytest.MonkeyPatch, authorization: str | None
) -> None:
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", False)
    headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
        )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid or revoked MCP client token"}


def test_cluster_secret_bearer_is_not_an_mcp_client_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": _ACCEPT,
                "Content-Type": "application/json",
                **bearer_header(_SECRET),
            },
        )
    assert resp.status_code == 401


def test_client_token_works_when_cluster_auth_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with TestClient(app) as client:
        created = client.post(
            "/api/mcp/clients",
            json={"name": "separate-auth-client", "scope": "read"},
            headers=bearer_header(_SECRET),
        )
        assert created.status_code == 200, created.text

        messages = _post(
            client,
            created.json()["token"],
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        listed = client.get("/api/mcp/clients", headers=bearer_header(_SECRET))

    assert messages[0]["result"]["tools"]
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["last_used_at"] is not None


def test_revoked_client_token_is_rejected() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/mcp/clients",
            json={"name": "revoked-client", "scope": "write"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        revoked = client.post(f"/api/mcp/clients/{body['id']}/revoke")
        assert revoked.status_code == 200, revoked.text

        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": _ACCEPT,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {body['token']}",
            },
        )

    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid or revoked MCP client token"}


# ── audit: every tools/call lands in the unified event stream ────────────


def test_tool_call_audit_identifies_client_and_redacts_args() -> None:
    prompt = "sensitive audit prompt"
    with TestClient(app) as client:
        created = client.post(
            "/api/mcp/clients",
            json={"name": "audit-write-client", "scope": "write"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        _tool_call(client, body["token"], "spawn_agent", {"prompt": prompt}, req_id=7)

    hits = _audit_hits("spawn_agent", "audit-write-client")
    assert hits, "no mcp_tool_call event for spawn_agent reached the mirror"
    attributes = hits[-1]["attributes"]
    encoded = json.dumps(prompt, ensure_ascii=False)
    assert attributes["client_id"] == body["id"]
    assert attributes["client_name"] == "audit-write-client"
    assert attributes["outcome"] == "ok"
    assert attributes["args"] == {
        "schema": {"prompt": "string"},
        "size": {"prompt": len(encoded)},
        "sha256": {"prompt": hashlib.sha256(encoded.encode()).hexdigest()},
    }
    assert prompt not in json.dumps(attributes, ensure_ascii=False)
    assert hits[-1]["category"] == "audit"
    assert hits[-1]["source"] == f"external_agent:mcp:{body['id']}"
    assert attributes["auth_principal"] == {"kind": "mcp_client", "id": body["id"]}
    assert attributes["caller_identity"] == {
        "kind": "external_agent",
        "subject": "mcp",
        "instance": str(body["id"]),
    }
    assert body["token"] not in json.dumps(attributes)
