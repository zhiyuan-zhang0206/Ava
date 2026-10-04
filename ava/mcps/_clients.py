"""The MCP clients one `AvaContext` holds: a background loop, its sessions, the daemon client.

MCP `ClientSession` / `stdio_client` are async-only and the SDK is sync, so a sync caller crosses
into a loop that lives as long as the context: `McpClients.run` hands a coroutine to it. The
sessions (one per server) live inside that loop; each one's transport context sits in its own
`AsyncExitStack` so a dead session can be closed individually and rebuilt. A per-server
`asyncio.Lock` keeps two callers from connecting to the same server at once.

The loop is anyio's blocking portal (a daemon thread the library owns), started on first use and
stopped by `close()`. Closing does not await the sessions' transports: closing stdio subprocesses
while the parent dies is prone to deadlock, so a compliant server sees EOF on the pipe the OS closes
at process end and exits by itself.

The set belongs to the context's `ClientSet` (`clients.get(McpClients)`); nothing here is
module-level state.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import AbstractContextManager, AsyncExitStack, suppress
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal

from . import _remote
from ._remote import _RemoteMCPClient, connect_remote


class McpClients:
    """Loop, sessions and daemon client of one context (see the module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._portal_cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self.sessions: dict[str, Any] = {}  # server -> ClientSession (async-only)
        self.session_locks: dict[str, asyncio.Lock] = {}
        # Per-server transport stack, so a dead session can be closed individually for rebuild.
        self.session_stacks: dict[str, AsyncExitStack] = {}
        self._remote: _RemoteMCPClient | None = None

    def run(self, coro: Any) -> Any:
        """Sync entry point: hand `coro` to the loop and wait for its result."""
        with self._lock:
            if self._portal is None:
                self._portal_cm = start_blocking_portal()
                self._portal = self._portal_cm.__enter__()
            portal = self._portal

        async def await_it() -> Any:
            return await coro

        return portal.call(await_it)

    def remote(self) -> _RemoteMCPClient | None:
        """The client of this machine's MCP daemon, or None when its socket is absent (the caller
        then connects locally). Built once and reused across calls."""
        socket_path = _remote._daemon_socket_path()
        if not socket_path:
            return None
        with self._lock:
            if self._remote is None:
                self._remote = connect_remote(socket_path)
            return self._remote

    def close(self) -> None:
        """Stop the loop and drop the daemon connection; cached sessions die with the process."""
        with self._lock:
            portal_cm, self._portal_cm, self._portal = self._portal_cm, None, None
            remote, self._remote = self._remote, None
            self.sessions.clear()
            self.session_locks.clear()
            self.session_stacks.clear()
        if remote is not None:
            remote.close()
        if portal_cm is not None:
            with suppress(RuntimeError):
                portal_cm.__exit__(None, None, None)
