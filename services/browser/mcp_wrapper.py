"""Per-agent chrome MCP bridge: stdio MCP server in front of the shared service.

Speaks the standard MCP protocol to one agent (over stdio) and forwards every
call over a Unix socket to the per-machine `services.browser.mcp_daemon`, which
owns the single `chrome-devtools-mcp` upstream attached to the shared headed
Chrome. The tool list passes through verbatim, so an upstream version bump
(new/renamed tools) surfaces automatically. This process is thin and per-agent;
the heavy upstream + the page-affinity / cold-start logic live in the daemon, so
one Chrome client is shared instead of one per agent.

Wired in ava_builtins/mcps/chrome/.mcp.json; takes no arguments (the socket path is derived
from settings, matching the daemon). The MCP daemon spawns it with cwd pinned to
the repo root (`ava/mcp_config.py:server_cwd`), so the relative interpreter path
resolves there — launched directly rather than through `uv run`, which would hang
one resident wrapper process per agent:

    .venv/bin/python -m services.browser.mcp_wrapper
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
    is_transport_error,
)
from shared.paths import chrome_mcp_socket

# The daemon explicitly rejected the request before forwarding it upstream.
_UPSTREAM_DOWN_MSG = "chrome upstream session is down; browser-mcp will restart"


def _retryable_rejection(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and _UPSTREAM_DOWN_MSG in str(exc)


class _Link(SocketLink):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        super().__init__(reader, writer, service_label="chrome MCP daemon", extra_fields=None)


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await dial_unix_socket(str(chrome_mcp_socket()), service_label="chrome MCP daemon")


class _ReconnectingLink(ReconnectingLink):
    def __init__(self, max_retries: int = 5, base_delay: float = 1.0) -> None:
        super().__init__(
            _connect,
            _Link,
            max_attempts=max_retries + 1,
            base_delay=base_delay,
            retryable_rejection=_retryable_rejection,
            close_on_error=is_transport_error,
        )


async def _serve() -> None:
    link = _ReconnectingLink()

    async def _list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        tools = await link.request({"method": "list_tools"})
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    # The upstream does its own schema validation; re-checking here would only
    # risk rejecting a call upstream would accept (SDK v2 performs no argument
    # validation of its own — the raw arguments pass through to the upstream).
    async def _call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = await link.request(
            {"method": "call_tool", "tool": params.name, "args": params.arguments or {}}
        )
        return types.CallToolResult.model_validate(result)

    server: Server[Any] = Server("chrome", on_list_tools=_list_tools, on_call_tool=_call_tool)

    async with stdio_server() as (server_read, server_write):
        await server.run(server_read, server_write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
