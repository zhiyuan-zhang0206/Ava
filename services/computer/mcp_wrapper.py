"""Per-agent computer MCP bridge: stdio MCP server in front of the shared service.

Speaks the standard MCP protocol to one agent (over stdio) and forwards every
call over a Unix socket to the per-machine `services.computer.mcp_daemon`, which
executes desktop actions through the signed permissions helper and audits them.
The tool list passes through verbatim from the daemon (single source of truth).
`agent_id` is stamped on every request from this process's identity, so the
audit stream carries the acting agent.

Wired in ava_builtins/mcps/computer_use/.mcp.json as the local-fallback command
(the MCP daemon's primary path dials the service directly — see
ava/_mcp_computer.py). Takes no arguments (the socket path is derived from
settings, matching the daemon). The MCP daemon spawns it with cwd pinned to the
repo root (`ava/mcp_config.py:server_cwd`), so the relative interpreter path
resolves there:

    .venv/bin/python -m services.computer.mcp_wrapper
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from services.browser.mcp_socket_bridge import (
    ReconnectingLink,
    SocketLink,
    dial_unix_socket,
)
from shared.paths import computer_mcp_socket


def _agent_id() -> int | None:
    """This process's agent identity (None when running outside an agent)."""
    import os

    raw = os.environ.get("AVA_AGENT_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


class _Link(SocketLink):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        super().__init__(
            reader,
            writer,
            service_label="computer MCP daemon",
            extra_fields=lambda: {"agent_id": _agent_id()},
        )


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await dial_unix_socket(str(computer_mcp_socket()), service_label="computer MCP daemon")


class _ReconnectingLink(ReconnectingLink):
    def __init__(self) -> None:
        super().__init__(
            _connect,
            _Link,
            max_attempts=6,
            base_delay=0.5,
            close_on_error=lambda _exc: True,
        )


async def _serve() -> None:
    link = _ReconnectingLink()

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
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
