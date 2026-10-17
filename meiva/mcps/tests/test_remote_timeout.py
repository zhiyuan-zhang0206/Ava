from unittest.mock import MagicMock

from meiva.mcps._tools import call_raw, list_tools
from meiva.mcps.tests._mcps_helpers import tool_source


def test_list_tools_uses_remote_when_available() -> None:
    current_timeout = [7.5]
    fake_remote = MagicMock()
    fake_remote.list_tools.return_value = [{"name": "t1", "description": "d", "input_schema": {}}]
    source = tool_source(lambda: fake_remote, timeout_seconds=lambda: current_timeout[0])

    for timeout in (7.5, 2.0):
        current_timeout[0] = timeout
        tools = list_tools(source, "srv")
        assert tools == [{"name": "t1", "description": "d", "input_schema": {}}]
        assert fake_remote.list_tools.call_args.args == ("srv",)
        assert fake_remote.list_tools.call_args.kwargs == {"timeout_seconds": timeout}
    assert fake_remote.list_tools.call_count == 2


def test_call_raw_uses_remote_when_available() -> None:
    current_timeout = [7.5]
    fake_remote = MagicMock()
    fake_remote.call_tool.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
        "structuredContent": None,
    }
    source = tool_source(lambda: fake_remote, timeout_seconds=lambda: current_timeout[0])

    for timeout in (7.5, 2.0):
        current_timeout[0] = timeout
        out = call_raw(source, "srv", "tool_x", arg="v")
        assert out["content"][0]["text"] == "ok"
        assert fake_remote.call_tool.call_args.args == ("srv", "tool_x", {"arg": "v"})
        assert fake_remote.call_tool.call_args.kwargs == {"timeout_seconds": timeout}
    assert fake_remote.call_tool.call_count == 2
