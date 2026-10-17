"""Live delivery into a running Codex app server over its websocket endpoint.

The relay's fast path is the app server's control plane: a websocket carrying
JSON-RPC (with the ``"jsonrpc"`` member omitted) over the server's unix socket,
or a ``ws://`` / ``wss://`` endpoint when the session recorded one.
``turn/start`` is the live verb — with an idle thread the server starts a fresh
turn, with an active steerable turn it steers that turn, and either way the
reply carries the turn: the message is in the host's flow now, not parked in a
database waiting for the next idle boundary.

A JSON-RPC error or transport failure is a delivery failure. The caller stops
the relay; it must not substitute ``codex queue``, whose Pending semantics wait
for the current turn to finish. Meiva's durable, unacknowledged inbox remains the
recovery source.

Wire facts verified against Codex 0.155.1: ``turn/start`` atomically starts an
idle turn or steers an active regular turn, without the read/steer race of
looking up ``expectedTurnId`` for ``turn/steer``. Every connection starts with
``initialize`` then ``initialized``. Requests omit ``jsonrpc``; replies match
by id and carry result/error, since server requests and notifications interleave.
The default daemon socket is
``$CODEX_HOME/app-server-control/app-server-control.sock``.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psutil
from websockets.sync.client import ClientConnection, unix_connect
from websockets.sync.client import connect as ws_connect

LIVE_SUBMIT_TIMEOUT_SECONDS = 5.0
"""Deadline for one whole live attempt: connect, handshake, and the turn/start.

The endpoint is a local socket (or a reachable host); when it answers, the
round trips are milliseconds, so the deadline only bites against a hung
endpoint. Failure stops the relay without acknowledging the durable inbox;
the bound stays well under the relay's 10s heartbeat cadence.
"""

_CLOSE_TIMEOUT_SECONDS = 1.0
_MIN_REMAINING_SECONDS = 0.05
_REASON_MAX_CHARS = 200
_CLIENT_INFO: dict[str, str] = {"name": "meiva-impersonation-relay", "version": "1"}
_INITIALIZE_REQUEST_ID = 1
TURN_REQUEST_ID = 2
_ANTIGRAVITY_SERVICE_PREFIX = "/exa.language_server_pb.LanguageServerService/"
_ANTIGRAVITY_PROBE_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class AntigravityServer:
    """Local Antigravity language_server Connect endpoint and CSRF token."""

    address: str
    csrf_token: str


def default_control_socket() -> Path:
    """The local codex daemon's control socket path.

    A bare ``--listen unix://`` binds here (verified on codex 0.153.4), and
    ``CODEX_HOME`` overrides ``~/.codex`` exactly as it does for codex itself.
    """
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path("~/.codex").expanduser()
    return base / "app-server-control" / "app-server-control.sock"


def default_control_endpoint() -> str | None:
    """The local daemon's endpoint once its socket exists, else None.

    Resolution does not prove the daemon owns the target thread. The request
    records this endpoint; the relay's first submission must still succeed.
    """
    socket_path = default_control_socket()
    return f"unix://{socket_path}" if socket_path.exists() else None


def require_control_endpoint(remote: str | None) -> str:
    """Resolve the existing host's Steer endpoint before acquiring control."""
    endpoint = remote or default_control_endpoint()
    if endpoint is None:
        raise RuntimeError(
            "Codex Steer delivery requires the existing session's app server; "
            "pass --codex-remote with the same endpoint used by codex --remote. "
            "Pending delivery via codex queue is not supported."
        )
    return f"unix://{default_control_socket()}" if endpoint == "unix://" else endpoint


def live_submit(
    thread_id: str,
    message: str,
    *,
    endpoint: str,
    timeout: float = LIVE_SUBMIT_TIMEOUT_SECONDS,
) -> str | None:
    """Try to hand one message to a running codex app server, live.

    Returns ``None`` when the server accepted the input (a JSON-RPC result:
    it started a turn or steered the active one). Otherwise returns a short
    reason — a refusal, an unreachable endpoint, a failed handshake, a timeout
    — for the caller to report as a delivery failure. Never
    raises for a failed delivery.

    ``endpoint`` takes the shapes codex itself accepts: ``unix://PATH`` (bare
    ``unix://`` means the default control socket), ``ws://host:port`` or
    ``wss://host:port``. Authentication headers are not sent; the recorded
    endpoint is expected to be reachable as-is.
    """
    deadline = time.monotonic() + timeout
    conn: ClientConnection | None = None
    try:
        conn = _open(endpoint, deadline)
        _initialize(conn, deadline)
        response = _request(
            conn,
            TURN_REQUEST_ID,
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": message}]},
            deadline,
        )
        error = response.get("error")
        if error is not None:
            return _clip(f"turn/start refused ({_error_summary(error)})")
        return None
    except Exception as exc:
        # A timeout may follow server acceptance. Do not retry through another
        # transport: only a controller ACK establishes receipt in Meiva.
        return _clip(f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()


def _extract_csrf_token(cmdline: list[str]) -> str | None:
    for idx, arg in enumerate(cmdline):
        if arg == "--csrf_token" and idx + 1 < len(cmdline) and cmdline[idx + 1].strip():
            return cmdline[idx + 1].strip()
        if arg.startswith("--csrf_token="):
            value = arg.removeprefix("--csrf_token=").strip()
            if value:
                return value
    return None


def _is_antigravity_language_server(name: str, cmdline: list[str]) -> bool:
    if name.lower().startswith("language_server"):
        return True
    if cmdline:
        exe_name = Path(cmdline[0]).name.lower()
        return exe_name.startswith("language_server")
    return False


def _process_candidate_servers(proc: psutil.Process) -> list[AntigravityServer]:
    try:
        info = proc.info  # type: ignore[attr-defined]
        cmdline = list(info.get("cmdline") or [])
        name = str(info.get("name") or "")
        if not _is_antigravity_language_server(name, cmdline):
            return []
        csrf_token = _extract_csrf_token(cmdline)
        if not csrf_token:
            return []
        ports = sorted(
            {
                int(conn.laddr.port)
                for conn in proc.net_connections(kind="inet")
                if conn.status == psutil.CONN_LISTEN and conn.laddr
            }
        )
    except (psutil.Error, OSError, ValueError):
        return []
    return [AntigravityServer(address=f"127.0.0.1:{port}", csrf_token=csrf_token) for port in ports]


def _probe_antigravity_server(candidate: AntigravityServer, timeout: float) -> bool:
    try:
        antigravity_rpc("GetWorkspaceInfos", {}, server=candidate, timeout=timeout)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def discover_antigravity_server(
    *, timeout: float = LIVE_SUBMIT_TIMEOUT_SECONDS
) -> AntigravityServer:
    """Resolve the local Antigravity language_server Connect endpoint and CSRF token."""
    env_address = (os.environ.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
    env_token = (os.environ.get("ANTIGRAVITY_CSRF_TOKEN") or "").strip()
    if env_address and env_token:
        return AntigravityServer(address=env_address, csrf_token=env_token)

    probe_timeout = min(timeout, _ANTIGRAVITY_PROBE_TIMEOUT_SECONDS)
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        for candidate in _process_candidate_servers(proc):
            if _probe_antigravity_server(candidate, probe_timeout):
                return candidate
    raise RuntimeError(
        "No running Antigravity language_server with an HTTP Connect endpoint was found on this machine"
    )


def antigravity_rpc(
    method: str,
    payload: Mapping[str, object],
    *,
    server: AntigravityServer | None = None,
    timeout: float = LIVE_SUBMIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Call one Connect JSON-RPC method on the local Antigravity language_server."""
    target = server or discover_antigravity_server(timeout=timeout)
    url = f"http://{target.address}{_ANTIGRAVITY_SERVICE_PREFIX}{method}"
    body = json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": target.csrf_token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(OSError, ValueError):
            detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Antigravity {method} failed (HTTP {exc.code}): {detail or exc.reason}"
        ) from exc
    parsed: object = json.loads(raw) if raw.strip() else {}
    mapping = _as_mapping(parsed)
    if mapping is None:
        raise RuntimeError(f"Antigravity {method} returned non-object JSON")
    return mapping


def antigravity_live_submit(
    conversation_id: str,
    message: str,
    *,
    server: AntigravityServer | None = None,
    timeout: float = LIVE_SUBMIT_TIMEOUT_SECONDS,
) -> str | None:
    """Deliver one message to an Antigravity conversation via SendAgentMessage.

    Returns ``None`` when the server accepted the message, or a short reason on
    failure. Never raises.
    """
    try:
        antigravity_rpc(
            "SendAgentMessage",
            {"recipient": conversation_id, "content": message},
            server=server,
            timeout=timeout,
        )
        return None
    except Exception as exc:
        return _clip(f"{type(exc).__name__}: {exc}")


def _open(endpoint: str, deadline: float) -> ClientConnection:
    remaining = _remaining(deadline)
    if endpoint.startswith("unix://"):
        path = endpoint.removeprefix("unix://") or str(default_control_socket())
        return unix_connect(
            path=path,
            open_timeout=remaining,
            close_timeout=_CLOSE_TIMEOUT_SECONDS,
            compression=None,
        )
    if endpoint.startswith(("ws://", "wss://")):
        # proxy=None keeps the dial as direct as codex's own client; an
        # environment-discovered proxy is not part of the recorded endpoint.
        return ws_connect(
            endpoint,
            open_timeout=remaining,
            close_timeout=_CLOSE_TIMEOUT_SECONDS,
            compression=None,
            proxy=None,
        )
    raise ValueError(f"unsupported app-server endpoint: {endpoint!r}")


def _initialize(conn: ClientConnection, deadline: float) -> None:
    response = _request(
        conn,
        _INITIALIZE_REQUEST_ID,
        "initialize",
        {"clientInfo": dict(_CLIENT_INFO)},
        deadline,
    )
    error = response.get("error")
    if error is not None:
        raise RuntimeError(f"initialize refused: {_error_summary(error)}")
    conn.send(json.dumps({"method": "initialized"}))


def _request(
    conn: ClientConnection,
    request_id: int,
    method: str,
    params: dict[str, object],
    deadline: float,
) -> dict[str, Any]:
    conn.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        message: object = json.loads(conn.recv(timeout=_remaining(deadline)))
        # A reply matches by id and carries result/error; notifications and
        # server-initiated requests interleave and are skipped.
        reply = _as_mapping(message)
        if (
            reply is not None
            and reply.get("id") == request_id
            and ("result" in reply or "error" in reply)
        ):
            return reply
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no {method} reply within the deadline")


def _as_mapping(value: object) -> dict[str, Any] | None:
    """One JSON-RPC message as a mapping, when it is one at all."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _error_summary(error: object) -> str:
    mapping = _as_mapping(error)
    if mapping is not None:
        return f"{mapping.get('code')}: {mapping.get('message')}"
    return str(error)


def _remaining(deadline: float) -> float:
    return max(_MIN_REMAINING_SECONDS, deadline - time.monotonic())


def _clip(reason: str) -> str:
    return reason[:_REASON_MAX_CHARS]
