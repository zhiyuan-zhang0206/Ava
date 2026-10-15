import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from ava.mcps import _daemon as daemon
from base.host.system.tests.fake_commands import fake_host


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


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("overlay", ['{"mcp_servers":{"fs":{"enabled":false}}}', "{not json"])
async def test_daemon_warm_session_rejects_changed_overlay(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, shared: bool, overlay: str
) -> None:
    import mcp.client.stdio

    _configure(unit_home, shared=shared)
    session = CountingSession()
    scope = daemon.Scope(
        local=daemon.Buckets(),
        shared=daemon.Buckets(),
        oauth_locks={},
        timeout_seconds=lambda: 15.0,
    )
    bucket = scope.shared if shared else scope.local
    bucket.sessions["fs"] = session
    spawn = MagicMock(side_effect=AssertionError("no server may start"))
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", spawn)
    request = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "bump"}}
    assert (await daemon.dispatch_with_retry(request, scope, host=fake_host()))["ok"] is True
    assert session.effects == 1
    (unit_home / "mcp_enabled.json").write_text(overlay)
    response = await daemon.dispatch_with_retry(request, scope, host=fake_host())
    assert response["ok"] is False
    reason = "mcp_enabled.json" if overlay == "{not json" else "not configured"
    assert reason in response["error"]
    assert session.effects == 1
    assert bucket.sessions["fs"] is session
    spawn.assert_not_called()
