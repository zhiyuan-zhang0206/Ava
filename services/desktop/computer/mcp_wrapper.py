"""Per-agent computer MCP bridge: stdio MCP server in front of the shared service.

Speaks the standard MCP protocol to one agent (over stdio) and forwards every
call over a Unix socket to the per-machine `services.desktop.computer.mcp_daemon`, which
executes desktop actions through the signed permissions helper and audits them.
The tool list passes through verbatim from the daemon (single source of truth).
`agent_id` is stamped on every request from this process's identity, so the
audit stream carries the acting agent.

Wired in ava_builtins/mcps/computer_use/.mcp.json as the local-fallback command
(the MCP daemon's primary path dials the service directly — see
ava/mcps/_computer.py). Takes no arguments (the socket path is derived from
settings, matching the daemon). The MCP daemon spawns it with cwd pinned to the
repo root (`ava/mcp_config.py:server_cwd`), so the relative interpreter path
resolves there:

    .venv/bin/python -m services.desktop.computer.mcp_wrapper
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from base.config import ConfigBoot
from base.paths import computer_mcp_socket
from services.desktop.browser.mcp_socket_bridge import (
    ReconnectingLink,
    SocketLink,
    dial_unix_socket,
)


def _agent_id() -> int | None:
    """This process's agent identity (None when running outside an agent)."""
    import os

    raw = os.environ.get("AVA_AGENT_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


class _Link(SocketLink):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        timeout_reader: Callable[[], float],
    ) -> None:
        super().__init__(
            reader,
            writer,
            service_label="computer MCP daemon",
            timeout_reader=timeout_reader,
            extra_fields=lambda: {"agent_id": _agent_id()},
        )


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await dial_unix_socket(str(computer_mcp_socket()), service_label="computer MCP daemon")


class _ReconnectingLink(ReconnectingLink):
    def __init__(self, *, timeout_reader: Callable[[], float]) -> None:
        super().__init__(
            _connect,
            lambda reader, writer: _Link(reader, writer, timeout_reader=timeout_reader),
            max_attempts=6,
            base_delay=0.5,
            close_on_error=lambda _exc: True,
        )


async def _serve(*, timeout_reader: Callable[[], float]) -> None:
    link = _ReconnectingLink(timeout_reader=timeout_reader)

    async def _list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        tools = await link.request({"method": "list_tools"})
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    async def _call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = await link.request(
            {"method": "call_tool", "tool": params.name, "args": params.arguments or {}}
        )
        return types.CallToolResult.model_validate(result)

    server: Server[Any] = Server("computer_use", on_list_tools=_list_tools, on_call_tool=_call_tool)

    async with stdio_server() as (server_read, server_write):
        await server.run(server_read, server_write, server.create_initialization_options())


def main() -> None:
    config = ConfigBoot()
    config.boot()
    asyncio.run(_serve(timeout_reader=lambda: config.view.sandbox.mcp_connect_timeout_seconds))


if __name__ == "__main__":
    main()
