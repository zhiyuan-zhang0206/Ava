"""Enable admission precedes session reuse, including previously acquired callables."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from ava import mcps
from ava.mcps import _daemon as daemon
from ava.mcps._clients import McpClients
from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.packages.plugins.mcp_enabled import McpEnabledConfigError


class CountingSession:
    def __init__(self) -> None:
        self.effects = 0

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(name="bump", input_schema={})])

    async def call_tool(self, name: str, arguments: object) -> CallToolResult:
        self.effects += 1
        return CallToolResult(content=[TextContent(type="text", text=f"effect {self.effects}")])


def _configure(home: Path, *, shared: bool = False) -> None:
    (home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fs": {"command": "must-not-run", "shared": shared}}})
    )


@pytest.mark.parametrize("overlay", ['{"mcp_servers":{"fs":{"enabled":false}}}', "{not json"])
def test_local_warm_callable_and_raw_reject_changed_overlay(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, overlay: str
) -> None:
    import mcp.client.stdio

    _configure(unit_home)
    context = AvaContext()
    session = CountingSession()
    spawn = MagicMock(side_effect=AssertionError("no server may start"))
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", spawn)

    def no_remote(_self: McpClients) -> None:
        return None

    monkeypatch.setattr(McpClients, "remote", no_remote)
    try:
        with process_context.scoped(context):
            clients = context.clients.get(McpClients)
            clients.sessions["fs"] = session
            before = mcps.fs.bump
            assert before() == "effect 1"
            (unit_home / "mcp_enabled.json").write_text(overlay)
            with pytest.raises(mcps.MCPCallError) as proxy_error:
                before()
            with pytest.raises(mcps.MCPCallError) as raw_error:
                mcps._call_raw("fs", "bump")
            error_type = McpEnabledConfigError if overlay == "{not json" else mcps.MCPServerNotFound
            assert isinstance(proxy_error.value.__cause__, error_type)
            assert isinstance(raw_error.value.__cause__, error_type)
            assert session.effects == 1
            assert clients.sessions["fs"] is session
            spawn.assert_not_called()
    finally:
        context.clients.close()


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("overlay", ['{"mcp_servers":{"fs":{"enabled":false}}}', "{not json"])
async def test_daemon_warm_session_rejects_changed_overlay(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, shared: bool, overlay: str
) -> None:
    import mcp.client.stdio

    _configure(unit_home, shared=shared)
    session = CountingSession()
    scope = daemon._Scope(local=daemon._Buckets(), shared=daemon._Buckets(), oauth_locks={})
    bucket = scope.shared if shared else scope.local
    bucket.sessions["fs"] = session
    spawn = MagicMock(side_effect=AssertionError("no server may start"))
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", spawn)
    request = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "bump"}}
    assert (await daemon._dispatch_with_retry(request, scope))["ok"] is True
    assert session.effects == 1
    (unit_home / "mcp_enabled.json").write_text(overlay)
    response = await daemon._dispatch_with_retry(request, scope)
    assert response["ok"] is False
    reason = "mcp_enabled.json" if overlay == "{not json" else "not configured"
    assert reason in response["error"]
    assert session.effects == 1
    assert bucket.sessions["fs"] is session
    spawn.assert_not_called()


@pytest.mark.parametrize("overlay", [None, '{"mcp_servers":{"fs":{"enabled":true}}}'])
async def test_enabled_local_and_daemon_sessions_remain_reusable(
    unit_home: Path, overlay: str | None
) -> None:
    _configure(unit_home)
    if overlay is not None:
        (unit_home / "mcp_enabled.json").write_text(overlay)
    clients = McpClients()
    session = CountingSession()
    clients.sessions["fs"] = session
    scope = daemon._Scope(local=daemon._Buckets(), shared=daemon._Buckets(), oauth_locks={})
    scope.local.sessions["fs"] = session
    assert await mcps._connect(clients, "fs") is session
    assert await daemon._get_session("fs", scope) is session
    assert await mcps._connect(clients, "fs") is session
    assert await daemon._get_session("fs", scope) is session
    assert session.effects == 0
