"""SDK composition keeps per-context MCP policy and lazy resource lifetimes."""

from pathlib import Path
from unittest.mock import MagicMock

import ava
from ava import mcps
from ava.mcps import McpClients
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.paths import mcp_daemon_shared_socket


def test_sdk_clients_use_separate_live_timeout_readers_and_rebuild_after_close() -> None:
    timeouts = {"first": 7.5, "second": 11.0}
    first = AvaContext(clients=process_clients(mcp_timeout_seconds=lambda: timeouts["first"]))
    second = AvaContext(clients=process_clients(mcp_timeout_seconds=lambda: timeouts["second"]))
    first_client = first.clients.get(McpClients)
    second_client = second.clients.get(McpClients)
    assert first_client is not second_client
    assert first_client._portal is None and second_client._portal is None
    first_remote, second_remote = MagicMock(), MagicMock()
    first_remote.list_tools.return_value = []
    second_remote.list_tools.return_value = []
    first_client._remote = first_remote
    second_client._remote = second_remote
    socket_path = Path(mcp_daemon_shared_socket())
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.touch()
    previous = ava.unbind_context()
    try:
        ava.bind_context(first)
        assert mcps._list_tools("test") == []
        ava.bind_context(second)
        assert mcps._list_tools("test") == []
        timeouts["first"] = 2.0
        ava.bind_context(first)
        assert mcps._list_tools("test") == []
        assert [call.kwargs for call in first_remote.list_tools.call_args_list] == [
            {"timeout_seconds": 7.5},
            {"timeout_seconds": 2.0},
        ]
        assert second_remote.list_tools.call_args.kwargs == {"timeout_seconds": 11.0}
        first.clients.close()
        first_remote.close.assert_called_once()
        rebuilt = first.clients.get(McpClients)
        assert rebuilt is not first_client
        assert rebuilt.timeout_seconds() == 2.0
        assert second_client.timeout_seconds() == 11.0
        assert rebuilt._portal is None
    finally:
        socket_path.unlink()
        ava.unbind_context()
        first.clients.close()
        second.clients.close()
        if previous is not None:
            ava.bind_context(previous)


def test_mcp_client_construction_does_not_read_configuration() -> None:
    def premature_read() -> float:
        raise AssertionError("configuration belongs to an operation or session creation")

    clients = process_clients(mcp_timeout_seconds=premature_read)
    assert not clients._made
    mcp = clients.get(McpClients)
    assert mcp._portal is None and not mcp.sessions
    clients.close()
