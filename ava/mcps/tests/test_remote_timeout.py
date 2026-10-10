"""Remote operations read each client owner's current timeout."""

from unittest.mock import MagicMock

import pytest

import ava.mcps as mcps_mod
from ava.mcps.tests._mcps_helpers import local_mcp_clients


def test_list_tools_uses_remote_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon available → directly use remote.list_tools, not reading cache / not connecting subprocess."""
    current_timeout = [7.5]
    local_mcp_clients(monkeypatch, lambda: current_timeout[0])
    fake_remote = MagicMock()
    fake_remote.list_tools.return_value = [{"name": "t1", "description": "d", "input_schema": {}}]
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    for timeout in (7.5, 2.0):
        current_timeout[0] = timeout
        tools = mcps_mod._list_tools("srv")
        assert tools == [{"name": "t1", "description": "d", "input_schema": {}}]
        assert fake_remote.list_tools.call_args.args == ("srv",)
        assert fake_remote.list_tools.call_args.kwargs == {"timeout_seconds": timeout}
    assert fake_remote.list_tools.call_count == 2


def test_call_raw_uses_remote_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    current_timeout = [7.5]
    local_mcp_clients(monkeypatch, lambda: current_timeout[0])
    fake_remote = MagicMock()
    fake_remote.call_tool.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
        "structuredContent": None,
    }
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    for timeout in (7.5, 2.0):
        current_timeout[0] = timeout
        out = mcps_mod._call_raw("srv", "tool_x", arg="v")
        assert out["content"][0]["text"] == "ok"
        assert fake_remote.call_tool.call_args.args == ("srv", "tool_x", {"arg": "v"})
        assert fake_remote.call_tool.call_args.kwargs == {"timeout_seconds": timeout}
    assert fake_remote.call_tool.call_count == 2
