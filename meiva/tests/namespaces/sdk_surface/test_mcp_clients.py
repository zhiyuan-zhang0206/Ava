from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from base.agents.context import MeivaContext
from base.paths import mcp_daemon_shared_socket, meiva_home
from meiva import mcps
from meiva.boot import process_clients
from meiva.mcps import McpClients
from meiva.sdk_surface import process_context


class _NoCache:
    def read(self, server: str) -> None:
        return None

    def write(self, server: str, tools: list[Any]) -> None:
        return None


def _list_tools(server: str) -> list[Any]:
    clients = process_context.current_context().clients.get(McpClients)
    source = mcps.call_funnel.ToolSource(
        remote=clients.remote,
        timeout_seconds=clients.timeout_seconds,
        cache=_NoCache(),
        validate_identity=lambda: None,
    )
    return mcps.call_funnel.list_tools(source, server)


def test_sdk_clients_use_separate_live_timeout_readers_and_rebuild_after_close() -> None:
    timeouts = {"first": 7.5, "second": 11.0}
    first = MeivaContext(clients=process_clients(mcp_timeout_seconds=lambda: timeouts["first"]))
    second = MeivaContext(clients=process_clients(mcp_timeout_seconds=lambda: timeouts["second"]))
    first_client = first.clients.get(McpClients)
    second_client = second.clients.get(McpClients)
    assert first_client is not second_client
    assert first_client._remote is None and second_client._remote is None
    first_remote, second_remote = MagicMock(), MagicMock()
    first_remote.list_tools.return_value = []
    second_remote.list_tools.return_value = []
    first_client._remote = first_remote
    second_client._remote = second_remote
    socket_path = Path(mcp_daemon_shared_socket(meiva_home()))
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.touch()
    previous = process_context.unbind_context()
    try:
        process_context.bind_context(first)
        assert _list_tools("test") == []
        process_context.bind_context(second)
        assert _list_tools("test") == []
        timeouts["first"] = 2.0
        process_context.bind_context(first)
        assert _list_tools("test") == []
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
        assert rebuilt._remote is None
    finally:
        socket_path.unlink()
        process_context.unbind_context()
        first.clients.close()
        second.clients.close()
        if previous is not None:
            process_context.bind_context(previous)


def test_mcp_client_construction_does_not_read_configuration() -> None:
    def premature_read() -> float:
        raise AssertionError("configuration belongs to an operation or session creation")

    clients = process_clients(mcp_timeout_seconds=premature_read)
    assert not clients._made
    mcp = clients.get(McpClients)
    assert mcp._remote is None
    clients.close()
