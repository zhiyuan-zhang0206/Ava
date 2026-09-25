"""A blocking client for the K1 control plane.

The server is asyncio; this client is a plain blocking socket — call sites
(tooling, tests) are synchronous, and one request/response pair per connection
is all the protocol needs. Transport failures raise RootClientError; business
failures come back as a `{"ok": false, ...}` response for the caller to read.
"""

from __future__ import annotations

import math
import socket
import sys
from pathlib import Path
from typing import cast

from services.ava_root.ipc import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    ResponsePayload,
    encode,
    parse_response,
)
from shared.proc_tree import OwnedProcess, leader_owns_pids

_DEFAULT_TIMEOUT_S = 30.0


class RootClientError(RuntimeError):
    """The root supervisor could not be reached, or answered with garbage."""


def native_identity(value: object) -> OwnedProcess:
    """Validate a persisted native birth from a root status row."""
    if not isinstance(value, dict):
        raise RootClientError("root status omitted native identity")
    row = cast("dict[str, object]", value)
    pid, birth, starttime = row.get("pid"), row.get("create_time"), row.get("starttime")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(birth, bool)
        or not isinstance(birth, (int, float))
        or birth <= 0
        or not math.isfinite(birth)
        or (
            starttime is not None
            and (isinstance(starttime, bool) or not isinstance(starttime, int) or starttime < 0)
        )
    ):
        raise RootClientError("root status lacks a captured native birth")
    return OwnedProcess(pid, float(birth), starttime)


def root_process(*, timeout: float = 2.0) -> OwnedProcess | None:
    """Read the root's captured native birth; IPC failure remains unknown."""
    from shared.paths import root_run_dir

    response = RootClient(root_run_dir() / "ava-root.sock", timeout=timeout).status()
    raw = response.get("result")
    if response["ok"] is not True or not isinstance(raw, dict):
        raise RootClientError("root refused its native status")
    body = cast("dict[str, object]", raw)
    root = native_identity(body.get("root"))
    return root if root.live() else None


def owned_process(unit_id: str, *, timeout: float = 2.0) -> OwnedProcess | None:
    """Read one captured generation and verify its live root ancestry.

    Unknown transport or birth evidence raises. An absent/stopped unit or a
    positively dead/reused birth returns None; no current PID occupant is adopted.
    """
    from shared.paths import root_run_dir

    response = RootClient(root_run_dir() / "ava-root.sock", timeout=timeout).status()
    raw = response.get("result")
    if response["ok"] is not True or not isinstance(raw, dict):
        raise RootClientError("root refused its native status")
    body = cast("dict[str, object]", raw)
    rows = body.get("units")
    if not isinstance(rows, list):
        raise RootClientError("root status omitted its unit roster")
    for item in cast("list[object]", rows):
        if not isinstance(item, dict):
            raise RootClientError("root status contains an invalid unit")
        row = cast("dict[str, object]", item)
        if row.get("id") != unit_id:
            continue
        if row.get("state") != "running":
            return None
        owner, root = native_identity(row), native_identity(body.get("root"))
        if not owner.live() or not root.live():
            return None
        if not leader_owns_pids(root, {owner.pid}):
            raise RootClientError("recorded service is outside its root ownership tree")
        return owner if owner.live() else None
    return None


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

    def force_down(self, name: str) -> ResponsePayload:
        """Stop an explicitly authorized execution domain, with bounded escalation."""
        return self.call("force-down", name)

    def restart(self, name: str) -> ResponsePayload:
        """Stop then replace `name` and its subtree."""
        return self.call("restart", name)

    def status(self) -> ResponsePayload:
        """Read the tree snapshot."""
        return self.call("status")

    def shutdown(self) -> ResponsePayload:
        """Ask the root daemon to close its tree and exit gracefully."""
        return self.call("shutdown")

    def resource(self, operation: str, payload: dict[str, object]) -> ResponsePayload:
        """Request an explicitly registered deployment resource operation."""
        try:
            return parse_response(
                self._roundtrip(
                    encode(
                        {
                            "verb": "resource",
                            "name": operation,
                            "payload": payload,
                        }
                    )
                )
            )
        except ProtocolError as exc:
            raise RootClientError(f"malformed resource response: {exc}") from exc

    def _roundtrip(self, payload: bytes) -> bytes:
        """One connect/send/read cycle; OSErrors become RootClientError."""
        try:
            if sys.platform == "win32":
                from services.ava_root.windows.transport import roundtrip

                raw, peer = roundtrip(self._socket_path, payload, self._timeout)
                response = parse_response(raw)
                body = response.get("result")
                if (
                    isinstance(body, dict)
                    and "root" in body
                    and native_identity(cast("dict[str, object]", body)["root"]).pid != peer
                ):
                    raise RootClientError("root status does not name the native pipe peer")
                return raw
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(str(self._socket_path))
                sock.sendall(payload)
                return _read_line(sock)
        except (OSError, ProtocolError, ValueError) as exc:
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
