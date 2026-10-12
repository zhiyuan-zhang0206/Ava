"""One lazy MCP daemon socket client per AvaContext."""

from __future__ import annotations

import threading
from collections.abc import Callable

from ava.mcp_config import MCPConnectError

from . import _remote
from ._remote import _RemoteMCPClient, connect_remote


class McpClients:
    """The context owns the daemon connection and its live timeout reader."""

    def __init__(self, timeout_seconds: Callable[[], float]) -> None:
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._remote: _RemoteMCPClient | None = None

    def remote(self) -> _RemoteMCPClient:
        """Require a daemon socket; never spawn an alternative tool runtime."""
        socket_path = _remote._daemon_socket_path()
        if not socket_path:
            raise MCPConnectError("MCP daemon unavailable; start the machine's mcp-daemon service")
        with self._lock:
            if self._remote is None:
                self._remote = connect_remote(socket_path)
            return self._remote

    def close(self) -> None:
        """Drop this context's daemon connection."""
        with self._lock:
            remote, self._remote = self._remote, None
        if remote is not None:
            remote.close()
