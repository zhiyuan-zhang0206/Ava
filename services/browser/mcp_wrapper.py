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
the repo root (`ava/_mcp_config.py:server_cwd`), so the relative interpreter path
resolves there — launched directly rather than through `uv run`, which would hang
one resident wrapper process per agent:

    .venv/bin/python -m services.browser.mcp_wrapper
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from services.browser.protocol import Response
from shared.config import settings
from shared.paths import chrome_mcp_socket

# A single tool result (screenshot / DOM snapshot) can be multi-MB on one line;
# lift the stream buffer cap well above StreamReader's 64KiB default.
_LINE_LIMIT = 64 * 1024 * 1024

# The daemon is a supervised cluster service that should already be up; a fresh
# agent may still race it on a cold cluster start, so retry the connect briefly
# before failing (which surfaces to the agent as the chrome server being down).
_CONNECT_ATTEMPTS = 10
_CONNECT_DELAY_S = 0.5

# Reconnect retry parameters for when the daemon connection drops (upstream
# chrome-devtools-mcp died and the daemon is restarting).
_RECONNECT_MAX_RETRIES = 5
_RECONNECT_BASE_DELAY_S = 1.0

# Transport errnos that signal a dead socket (same set as ava._mcps_daemon).
_TRANSPORT_ERRNOS = frozenset({32, 54, 61})  # EPIPE, ECONNRESET, ECONNREFUSED

# The error message the daemon returns when its upstream dies. When the wrapper
# sees this, it treats the connection as dead and reconnects (the daemon will
# auto-reconnect its upstream and start answering again).
_UPSTREAM_DOWN_MSG = "chrome upstream session is down; browser-mcp will restart"


class _NotDeliveredError(Exception):
    """The request never reached the daemon (the write/drain failed).

    Raised by ``_Link.request`` when the payload did not leave this process,
    so a retry is guaranteed safe — the daemon cannot have executed it.
    Every other transport failure after a successful drain is "delivered,
    response unknown" and must NOT be retried (audit round 2, P1).
    """


class _Link:
    """One Unix-socket connection to the daemon, serializing request/response."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self._id = 0

    async def request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            self._id += 1
            payload = {"id": self._id, **payload}

            async def _roundtrip() -> Any:
                try:
                    self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                    await self._writer.drain()
                except TimeoutError:
                    # drain stalled — the payload may be in flight; unknown
                    # outcome, so the caller must not retry.
                    raise
                except (ConnectionError, OSError) as e:
                    # The socket died before the payload left this process —
                    # the daemon cannot have executed it; retrying is safe.
                    raise _NotDeliveredError from e
                line = await self._reader.readline()
                if not line:
                    raise ConnectionError("chrome MCP daemon closed the connection")
                resp: Response = json.loads(line)
                # The lock serializes one full round-trip, so ids must arrive in order;
                # a mismatch means the stream desynced -- fail loud, never hand back a
                # different call's result.
                if resp.get("id") != self._id:
                    raise RuntimeError(
                        f"chrome MCP daemon response id {resp.get('id')} != request {self._id}"
                    )
                # `is False` (not truthiness) is what narrows the response union to
                # the ok branch, so `resp["result"]` below is a present key.
                if resp["ok"] is False:
                    raise RuntimeError(resp.get("error", "chrome MCP daemon error"))
                return resp["result"]

            # Bound the whole round-trip: a wedged daemon must surface as a
            # transport error (the reconnecting wrapper retries on TimeoutError)
            # instead of hanging the calling agent forever. The SDK/socket layer
            # has no default timeout of its own.
            return await asyncio.wait_for(
                _roundtrip(), timeout=settings.sandbox.mcp_connect_timeout_seconds
            )

    def close(self) -> None:
        """Close the underlying writer (best-effort)."""
        with suppress(Exception):
            self._writer.close()


class _ReconnectingLink:
    """Wraps _Link with automatic reconnect on transport errors.

    When the shared daemon restarts (Chrome restart, npx crash, OOM), the
    existing Unix-socket connection dies.  This wrapper transparently
    reconnects and retries instead of surfacing a transport error to the
    agent.  A successful request resets the retry counter — only consecutive
    failures trigger backoff, so a long-running wrapper survives occasional
    daemon restarts without the agent ever knowing.
    """

    def __init__(
        self,
        max_retries: int = _RECONNECT_MAX_RETRIES,
        base_delay: float = _RECONNECT_BASE_DELAY_S,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._link: _Link | None = None
        self._lock = asyncio.Lock()

    async def _connect_once(self) -> _Link:
        reader, writer = await _connect()
        return _Link(reader, writer)

    @staticmethod
    def _is_transport_error(exc: BaseException) -> bool:
        """Return True when *exc* indicates the daemon connection is dead."""
        name = type(exc).__name__
        if name in (
            "ConnectionError",
            "BrokenPipeError",
            "ConnectionResetError",
            "ConnectionRefusedError",
            "TimeoutError",
        ):
            return True
        # RuntimeError with the upstream-down message means the daemon's upstream
        # died and it is (or will be) reconnecting — treat as transport error so
        # the wrapper reconnects instead of surfacing to the agent.
        if name == "RuntimeError" and _UPSTREAM_DOWN_MSG in str(exc):
            return True
        if isinstance(exc, OSError):
            return getattr(exc, "errno", None) in _TRANSPORT_ERRNOS
        return False

    async def request(self, payload: dict[str, Any]) -> Any:
        """Forward to the daemon, reconnecting + retrying only safe retries.

        Browser tools are not idempotent (a click / submit / navigate run
        twice is a real double action), so the retry policy must distinguish
        "the daemon never saw the request" from "the daemon may have executed
        it and the response was lost" (audit round 2, P1):

        - connect-phase failure (nothing written yet): retry.
        - ``_NotDeliveredError`` (write/drain failed before the payload left):
          retry — nothing was executed.
        - the daemon's ``_UPSTREAM_DOWN_MSG`` rejection: the daemon refused
          to forward because its upstream died — NOT executed; retry after
          reconnect.
        - timeout / connection loss after delivery: outcome unknown — close
          the dead link so the NEXT call reconnects, and surface this one.
        """
        async with self._lock:
            for attempt in range(self._max_retries + 1):
                if self._link is None:
                    try:
                        self._link = await self._connect_once()
                    except Exception:
                        # Nothing was sent on this attempt — safe to retry.
                        if attempt == self._max_retries:
                            raise
                        await asyncio.sleep(self._base_delay * (2**attempt))
                        continue
                try:
                    return await self._link.request(payload)
                except _NotDeliveredError:
                    # The daemon never saw the request — retrying is safe.
                    self._link.close()
                    self._link = None
                    if attempt == self._max_retries:
                        raise
                    await asyncio.sleep(self._base_delay * (2**attempt))
                except Exception as e:
                    if isinstance(e, RuntimeError) and _UPSTREAM_DOWN_MSG in str(e):
                        # Explicit rejection, nothing executed (the daemon's
                        # upstream died and it is reconnecting) — retry.
                        self._link.close()
                        self._link = None
                        if attempt == self._max_retries:
                            raise
                        await asyncio.sleep(self._base_delay * (2**attempt))
                        continue
                    if not self._is_transport_error(e):
                        raise
                    # Delivered, response unknown (timeout / connection died
                    # mid-round-trip): do NOT retry — the call may have run.
                    # Close the dead link so the next request reconnects.
                    self._link.close()
                    self._link = None
                    raise
            # Unreachable — the loop always returns or raises.
            raise RuntimeError("unreachable")


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    sock = str(chrome_mcp_socket())
    last: Exception | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            return await asyncio.open_unix_connection(path=sock, limit=_LINE_LIMIT)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last = e
            await asyncio.sleep(_CONNECT_DELAY_S)
    raise ConnectionError(f"chrome MCP daemon not reachable at {sock}: {last}")


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
