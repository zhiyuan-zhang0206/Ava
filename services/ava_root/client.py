"""A blocking client for the K1 control plane.

The server is asyncio; this client is a plain blocking socket — call sites
(tooling, tests) are synchronous, and one request/response pair per connection
is all the protocol needs. Transport failures raise RootClientError; business
failures come back as a `{"ok": false, ...}` response for the caller to read.
"""

from __future__ import annotations

import socket
from pathlib import Path

from services.ava_root.ipc import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    ResponsePayload,
    encode,
    parse_response,
)

_DEFAULT_TIMEOUT_S = 30.0


class RootClientError(RuntimeError):
    """The root supervisor could not be reached, or answered with garbage."""


class RootClient:
    """One blocking call per method, one connection per call."""

    def __init__(self, socket_path: Path, *, timeout: float = _DEFAULT_TIMEOUT_S) -> None:
        self._socket_path = socket_path
        self._timeout = timeout

    def call(self, verb: str, name: str | None = None) -> ResponsePayload:
        """Send one verb and return the parsed response."""
        request: dict[str, object] = {"verb": verb}
        if name is not None:
            request["name"] = name
        raw = self._roundtrip(encode(request))
        try:
            return parse_response(raw)
        except ProtocolError as exc:
            raise RootClientError(f"malformed response from root supervisor: {exc}") from exc

    def up(self, name: str) -> ResponsePayload:
        """Bring `name` and its subtree up."""
        return self.call("up", name)

    def down(self, name: str) -> ResponsePayload:
        """Take `name` and its subtree down."""
        return self.call("down", name)

    def restart(self, name: str) -> ResponsePayload:
        """Rolling-replace `name` and its subtree."""
        return self.call("restart", name)

    def status(self) -> ResponsePayload:
        """Read the tree snapshot."""
        return self.call("status")

    def upgrade(self) -> ResponsePayload:
        """Ask for a supervisor upgrade (a stub in this slice)."""
        return self.call("upgrade")

    def _roundtrip(self, payload: bytes) -> bytes:
        """One connect/send/read cycle; OSErrors become RootClientError."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(str(self._socket_path))
                sock.sendall(payload)
                return _read_line(sock)
        except OSError as exc:
            raise RootClientError(
                f"root supervisor unreachable at {self._socket_path}: {exc}"
            ) from exc


def _read_line(sock: socket.socket) -> bytes:
    """Read one newline-terminated message, capped at the wire limit."""
    buffer = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            if not buffer:
                raise RootClientError("connection closed before any response")
            raise RootClientError("connection closed mid-response")
        index = chunk.find(b"\n")
        if index != -1:
            buffer.extend(chunk[:index])
            return bytes(buffer)
        buffer.extend(chunk)
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise RootClientError(f"response exceeded the {MAX_MESSAGE_BYTES}-byte message cap")
