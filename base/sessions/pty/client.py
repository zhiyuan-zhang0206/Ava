"""Client of the pty-sessions service: what agent, schedule and page-server processes dial.

One short connection per request over the service's unix socket, so an agent or
agent host restarting simply dials again. It stays light (sockets and the
closure's data types): it runs inside every process that touches a shell.

Queries (`has_session`, `list_sessions`, `live_sessions`) read a service that is
not running as "no sessions", which is true: its sessions ended with it.
Mutating requests raise `ServiceUnavailableError` instead, after a bounded wait for a
service that is still coming up (a start races its own clients).
"""

from __future__ import annotations

import base64
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from base.sessions.pty import protocol
from base.sessions.pty.closure import Outcome
from base.sessions.pty.paths import CAPTURE_MAX_LINES, service_socket_path
from base.sessions.record import SessionRecord

# Long enough to cover the service's longest single-session op (a graceful kill
# waits up to its own bound before escalating); connecting stays instant.
REQUEST_TIMEOUT_S = 30.0

# How long a mutating request waits for a service that is not accepting yet.
CONNECT_WAIT_S = 5.0

_CONNECT_RETRY_S = 0.1


class ServiceUnavailableError(OSError):
    """The pty-sessions service did not answer: it is down, or wedged."""


class ServiceDownError(ServiceUnavailableError):
    """No pty-sessions service is listening: its socket is absent or refuses connections.

    Nothing can be alive in a service that is not there, which is what the queries
    below read it as.
    """


class ServiceError(RuntimeError):
    """The service refused or failed a request; `code` is its protocol code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionInfo:
    """One live session as `list` reports it."""

    name: str
    pid: int
    create_time: float
    starttime: int | None
    cmd: str
    cwd: str
    started_at: float
    generation: str | None
    initial_command: str | None = None

    def record(self) -> SessionRecord:
        return SessionRecord(
            self.pid,
            self.create_time,
            self.cmd,
            self.cwd,
            self.started_at,
            self.starttime,
            self.generation,
        )


@dataclass(frozen=True)
class KillVerdict:
    """A terminal kill verdict; survivors name only known observed leftovers."""

    mode: str
    interrupted: bool
    survivors: tuple[int, ...] = ()


def _connect(wait_s: float, socket_path: Path | None) -> socket.socket:
    path = str(service_socket_path() if socket_path is None else socket_path)
    deadline = time.monotonic() + wait_s
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            conn.close()
            if time.monotonic() >= deadline:
                raise ServiceDownError(f"no pty-sessions service is listening at {path}") from exc
            time.sleep(_CONNECT_RETRY_S)
        except OSError:
            conn.close()
            raise
        else:
            return conn


def request(
    method: str,
    *,
    timeout: float = REQUEST_TIMEOUT_S,
    wait: float = 0.0,
    socket_path: Path | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Send one request and return the response's data.

    `socket_path` names the service's socket explicitly (a read-only scan that must
    not create the home); the default is this process's own.

    Raises:
        ServiceDownError: no service listens (after waiting up to `wait` seconds for
            one to accept).
        ServiceUnavailableError: a service was dialed and did not answer.
        ServiceError: the service refused or failed the request.
    """
    req_id = uuid.uuid4().hex
    conn = _connect(wait, socket_path)
    with conn:
        conn.settimeout(timeout)
        try:
            conn.sendall(protocol.encode({"id": req_id, "method": method, **fields}))
            line = protocol.read_line(conn, limit=1 << 30)
        except (TimeoutError, OSError, ValueError) as exc:
            raise ServiceUnavailableError(f"pty-sessions {method} got no answer: {exc}") from exc
    if line is None:
        raise ServiceUnavailableError(
            f"pty-sessions closed the connection without answering {method}"
        )
    try:
        response = protocol.decode_object(line)
    except (ValueError, TypeError) as exc:
        raise ServiceUnavailableError(f"pty-sessions returned a malformed response: {exc}") from exc
    if response.get("id") != req_id:
        raise ServiceUnavailableError(
            f"pty-sessions answered request {req_id} with id {response.get('id')!r}"
        )
    if not response.get("ok"):
        raise ServiceError(int(response.get("code") or protocol.ERROR), str(response.get("error")))
    data = response.get("data")
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}


def has_session(name: str) -> bool:
    try:
        return bool(request("has", name=name)["alive"])
    except ServiceDownError:
        return False


def list_sessions(
    prefix: str = "", *, socket_path: Path | None = None, include_initial_command: bool = False
) -> list[SessionInfo]:
    """Every live session whose name starts with `prefix`, sorted by name."""
    try:
        fields: dict[str, Any] = {"prefix": prefix}
        if include_initial_command:
            fields["include_initial_command"] = True
        rows = request("list", socket_path=socket_path, **fields)["sessions"]
    except ServiceDownError:
        return []
    return [SessionInfo(**row) for row in rows]


def live_sessions(prefix: str = "") -> dict[str, SessionRecord]:
    """Every live session's record by name: the shape the page server reconciles over."""
    return {info.name: info.record() for info in list_sessions(prefix)}


def create_session(name: str, cwd: str, env: dict[str, str], cmd: str | None = None) -> bool:
    """Create the session; True when this call created it, False when it already lived.

    Raises:
        ServiceError: the service refused (its message names the reason).
        ServiceUnavailableError: no service answers.
    """
    fields: dict[str, Any] = {"name": name, "cwd": cwd, "env": env}
    if cmd:
        fields["cmd"] = cmd
    return bool(request("new", wait=CONNECT_WAIT_S, **fields)["created"])


def send(name: str, data: bytes) -> None:
    request("send", name=name, data=base64.b64encode(data).decode("ascii"))


def capture(name: str, lines: int, *, scrollback: bool) -> str:
    if not 1 <= lines <= CAPTURE_MAX_LINES:
        raise ValueError(f"capture lines out of range: {lines}")
    return str(request("capture", name=name, lines=lines, scrollback=scrollback)["text"])


def resize(name: str, cols: int, rows: int) -> None:
    request("resize", name=name, cols=cols, rows=rows)


def kill(name: str, *, graceful: bool) -> KillVerdict:
    """End the session and report whether it cut running work short (idempotent).

    Without a service there is no session to end: a noop, like killing an absent one.
    """
    try:
        data = request("kill", name=name, graceful=graceful)
    except ServiceDownError:
        return KillVerdict("noop", interrupted=False)
    return KillVerdict(
        str(data["mode"]), bool(data["interrupted"]), tuple(data.get("survivors", ()))
    )


def close_all(*, grace_s: float, kill_s: float) -> Outcome:
    """Close known shells/terminals best effort; no descendant-absence proof."""
    data = request(
        "close_all",
        timeout=grace_s + 4 * kill_s + REQUEST_TIMEOUT_S,
        grace_s=grace_s,
        kill_s=kill_s,
    )
    return Outcome.from_wire(data)
