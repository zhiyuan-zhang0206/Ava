"""`ava mcp serve` — the MCP server wrapping this cluster's gateway.

The server is a proxy, so its whole contract is (a) which tools it advertises to
an external agent and what their arguments are, and (b) that each tool hits the
right gateway route with the right body and turns the answer into something a
model can act on. Both are verified here against a fake HTTP transport — no
gateway, no cluster.

`cli.mcp_server` imports `httpx` at module scope and `shared.machine` inside
`_request`, so patching `cli.mcp_server.httpx.AsyncClient` and
`shared.machine.gateway_api_base` both land at call time.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from cli import mcp_server

# The tools an external agent (Claude Code / Codex) sees. Pinned as a set: adding
# one is a deliberate widening of what a third party can do to the fleet, and
# removing one breaks every client whose config already names it.
_EXPECTED_TOOLS = {
    "list_agents",
    "get_agent",
    "spawn_agent",
    "send_message",
    "get_messages",
    "terminate_agent",
    "cluster_status",
}

# Matches any CJK character. Tool descriptions are read by a third party's model,
# not by this repo's maintainers, so they stay in English (CLAUDE.md principle 6).
_CJK = re.compile("[\u4e00-\u9fff\u3040-\u30ff]")


def _agent_row(agent_id: int = 7, status: str = "running") -> dict[str, Any]:
    """A full /api/agents row — more fields than `list_agents` returns, which is
    the point: the compaction is under test."""
    return {
        "agent_id": agent_id,
        "spawner": "mcp",
        "fork_source_agent_id": None,
        "fork_source_checkpoint_id": None,
        "status": status,
        "pid": 4242,
        "spawned_at": "2026-07-01T00:00:00Z",
        "started_at": "2026-07-01T00:00:01Z",
        "last_active_at": "2026-07-01T00:05:00Z",
        "label": "researcher",
        "machine": "mac-mini",
        "supports_vision": True,
        "notices_awaiting_response": [],
        "unread_notice_count": 0,
        "heartbeat_paused_until": None,
    }


def _summary_agent_row(agent_id: int = 7, status: str = "running") -> dict[str, Any]:
    """The list projection contains the fields the MCP compaction reads."""
    return {
        "agent_id": agent_id,
        "status": status,
        "label": "researcher",
        "machine": "mac-mini",
        "spawner": "mcp",
        "last_active_at": "2026-07-01T00:05:00Z",
    }


@pytest.fixture(autouse=True)
def _gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(
        "shared.machine.gateway_auth_headers", lambda: {"Authorization": "Bearer s"}
    )


def _patch_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> dict[str, httpx.Request]:
    """Route every dial through a MockTransport running `handler`; capture the
    outgoing request so a test can assert on URL / method / body."""
    captured: dict[str, httpx.Request] = {}
    real_client = httpx.AsyncClient  # captured before patching, to avoid recursion

    def _capturing(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return handler(request)

    def _factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(_capturing), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _factory)
    return captured


async def _call(name: str, args: dict[str, Any]) -> Any:
    """Invoke one tool through the real MCP path (argument validation included)
    and return its structured result.

    FastMCP answers `(unstructured, structured)`; a tool returning something
    other than an object has its value wrapped under `result` in the structured
    half, which is what a client receives — so tests assert on that shape rather
    than on the Python return value.
    """
    result = await mcp_server.build_server().call_tool(name, args)
    if not isinstance(result, CallToolResult):
        raise TypeError(f"unexpected result type {type(result).__name__}")
    return result.structured_content


# ─── tool surface ─────────────────────────────────────────────────────────


async def test_advertises_exactly_the_control_tools() -> None:
    tools = await mcp_server.build_server().list_tools()
    assert {t.name for t in tools} == _EXPECTED_TOOLS


async def test_tool_arguments_match_the_gateway_surface() -> None:
    """Each tool's declared arguments — what a client can actually pass."""
    schemas = {t.name: t.input_schema for t in await mcp_server.build_server().list_tools()}

    assert schemas["spawn_agent"]["required"] == ["prompt"]
    assert set(schemas["spawn_agent"]["properties"]) == {
        "prompt",
        "label",
        "machine",
        "config_overlay",
    }
    assert set(schemas["send_message"]["required"]) == {"agent_id", "content"}
    assert schemas["list_agents"].get("required", []) == []
    assert set(schemas["terminate_agent"]["properties"]) == {"agent_id", "message", "force"}
    assert schemas["cluster_status"].get("required", []) == []


async def test_descriptions_are_english_and_flag_the_destructive_tool() -> None:
    """The descriptions are the only contract the external model reads."""
    tools = {t.name: (t.description or "") for t in await mcp_server.build_server().list_tools()}
    assert all(t for t in tools.values()), "every tool needs a description"
    assert not [n for n, d in tools.items() if _CJK.search(d)]
    assert "DESTRUCTIVE" in tools["terminate_agent"]


# ─── each tool proxies to its gateway route ───────────────────────────────


async def test_list_agents_compacts_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(200, json=[_summary_agent_row()]))
    result = await _call("list_agents", {})

    assert str(captured["request"].url) == "http://gw:8000/api/agents?fields=summary"
    assert captured["request"].method == "GET"
    assert result == {
        "result": [
            {
                "agent_id": 7,
                "status": "running",
                "label": "researcher",
                "machine": "mac-mini",
                "spawner": "mcp",
                "last_active_at": "2026-07-01T00:05:00Z",
            }
        ]
    }


async def test_list_agents_filters_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_agent_row(1, "running"), _agent_row(2, "terminated")]
    _patch_http(monkeypatch, lambda _r: httpx.Response(200, json=rows))
    result = await _call("list_agents", {"status": "terminated"})
    assert [r["agent_id"] for r in result["result"]] == [2]


async def test_unknown_status_filter_errors_instead_of_returning_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list would read as "the fleet is empty" and get acted on; the
    error names the legal states so the caller can fix its own argument. Checked
    before the dial, so a bad filter costs no round trip."""

    def _unreached(_r: httpx.Request) -> httpx.Response:
        pytest.fail("must not dial the gateway with an invalid filter")

    _patch_http(monkeypatch, _unreached)
    with pytest.raises(ToolError, match=r"unknown agent status 'active'.*idling"):
        await _call("list_agents", {"status": "active"})


async def test_get_agent_returns_the_full_row(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(200, json=_agent_row()))
    result = await _call("get_agent", {"agent_id": 7})
    assert str(captured["request"].url) == "http://gw:8000/api/agents/7"
    assert result["pid"] == 4242, "get_agent is the un-compacted view"


async def test_spawn_agent_posts_prompt_with_mcp_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn carries `spawner=mcp` (so the fleet views group these agents)
    and a legal envelope source for the prompt — an illegal one is a 422."""
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(201, json={"agent_id": 11}))
    result = await _call(
        "spawn_agent",
        {"prompt": "audit the migrations", "label": "auditor", "machine": "wsl"},
    )

    req = captured["request"]
    assert str(req.url) == "http://gw:8000/api/agents"
    assert req.method == "POST"
    assert json.loads(req.content) == {
        "prompt": "audit the migrations",
        "prompt_source": "user",
        "spawner": "mcp",
        "label": "auditor",
        "machine": "wsl",
        "config": None,
    }
    assert result == {"agent_id": 11}


async def test_spawn_agent_forwards_the_config_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(201, json={"agent_id": 12}))
    await _call(
        "spawn_agent",
        {"prompt": "go", "config_overlay": {"llm_model": "claude-opus-5"}},
    )
    assert json.loads(captured["request"].content)["config"] == {"llm_model": "claude-opus-5"}


async def test_send_message_posts_as_user_source(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(201, json={"status": "idling"}))
    result = await _call("send_message", {"agent_id": 7, "content": "status?"})

    assert str(captured["request"].url) == "http://gw:8000/api/agents/7/messages"
    assert json.loads(captured["request"].content) == {"content": "status?", "source": "user"}
    assert result == {"status": "idling"}


async def test_get_messages_projects_role_text_and_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript read must carry the code the agent ran — that is the action."""
    payload = {
        "messages": [
            {"type": "human", "content": "count the migrations"},
            {
                "type": "ai",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "counting now"},
                ],
                "tool_calls": [{"name": "execute_code", "args": {"code": "print(1)"}}],
            },
        ],
        "msg_count": 40,
        "start_index": 38,
    }
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(200, json=payload))
    result = await _call("get_messages", {"agent_id": 7, "limit": 2})

    assert captured["request"].url.params["limit"] == "2"
    assert result == {
        "messages": [
            {"role": "human", "text": "count the migrations"},
            {"role": "ai", "text": "counting now", "code": ["print(1)"]},
        ],
        "total": 40,
    }


async def test_get_messages_defaults_to_a_bounded_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """No limit from the caller still bounds the read — an unbounded default
    would dump a months-long history into the client's context."""
    captured = _patch_http(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"messages": [], "msg_count": 0, "start_index": 0}),
    )
    await _call("get_messages", {"agent_id": 7})
    assert captured["request"].url.params["limit"] == str(mcp_server._DEFAULT_MESSAGE_LIMIT)


@pytest.mark.parametrize("force", [False, True])
async def test_terminate_agent_forwards_message_and_force(
    monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(200, json={"status": "enqueued"}))
    await _call(
        "terminate_agent",
        {"agent_id": 7, "message": "retain this result", "force": force},
    )

    assert str(captured["request"].url) == "http://gw:8000/api/agents/7/terminate"
    assert json.loads(captured["request"].content) == {
        "message": "retain this result",
        "force": force,
    }


async def test_cluster_status_reads_the_cluster_route(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_http(
        monkeypatch, lambda _r: httpx.Response(200, json={"machine_name": "mac", "paused": True})
    )
    result = await _call("cluster_status", {})
    assert str(captured["request"].url) == "http://gw:8000/api/cluster/status"
    assert result["paused"] is True


async def test_every_call_presents_the_cluster_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway's authenticated surface — an unauthenticated proxy would 401
    on every tool."""
    captured = _patch_http(monkeypatch, lambda _r: httpx.Response(200, json=[]))
    await _call("list_agents", {})
    assert captured["request"].headers["Authorization"] == "Bearer s"


# ─── gateway errors reach the client as tool errors ───────────────────────


async def test_missing_agent_becomes_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 must not read as "no messages" — the caller has to see the id is wrong."""
    _patch_http(
        monkeypatch,
        lambda _r: httpx.Response(404, json={"detail": "agent 99 does not exist", "reason": "x"}),
    )
    with pytest.raises(ToolError, match="agent 99 does not exist"):
        await _call("get_agent", {"agent_id": 99})


async def test_validation_error_names_the_offending_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic's 422 body is a list of per-field errors; flattening it is what
    lets the calling model fix its own argument and retry."""
    body = {"detail": [{"loc": ["body", "prompt"], "msg": "string too short"}]}
    _patch_http(monkeypatch, lambda _r: httpx.Response(422, json=body))
    with pytest.raises(ToolError, match=r"body\.prompt: string too short"):
        await _call("spawn_agent", {"prompt": "x"})


async def test_non_json_error_body_still_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, lambda _r: httpx.Response(502, text="upstream down"))
    with pytest.raises(ToolError, match="upstream down"):
        await _call("cluster_status", {})


async def test_unreachable_gateway_becomes_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_http(monkeypatch, _boom)
    with pytest.raises(ToolError, match="gateway unreachable at http://gw:8000/api/agents"):
        await _call("list_agents", {})


# ─── the command entry point ──────────────────────────────────────────────


def test_serve_fails_before_starting_when_no_gateway_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured checkout is an install problem. Raising here puts the
    message on the operator's terminal; starting anyway would bury it in a tool
    error the client surfaces much later."""
    from shared.machine import GatewayApiBaseMissing

    def _missing() -> str:
        raise GatewayApiBaseMissing("gateway_url unset")

    monkeypatch.setattr("shared.machine.gateway_api_base", _missing)
    monkeypatch.setattr(mcp_server, "build_server", lambda: pytest.fail("must not build a server"))
    with pytest.raises(GatewayApiBaseMissing):
        mcp_server.cmd_mcp_serve()


def test_serve_runs_the_server_on_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio is not a default worth drifting on: the whole point is that a
    client spawns `ava mcp serve` as a subprocess and talks over its pipes."""
    seen: dict[str, object] = {}

    class _FakeServer:
        def run(self, transport: str) -> None:
            seen["transport"] = transport

    monkeypatch.setattr(mcp_server, "build_server", _FakeServer)
    assert mcp_server.cmd_mcp_serve() == 0
    assert seen["transport"] == "stdio"
