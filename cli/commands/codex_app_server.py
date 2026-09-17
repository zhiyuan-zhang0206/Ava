"""Live delivery into a running Codex app server over its websocket endpoint.

The relay's fast path is the app server's control plane: a websocket carrying
JSON-RPC (with the ``"jsonrpc"`` member omitted) over the server's unix socket,
or a ``ws://`` / ``wss://`` endpoint when the session recorded one.
``turn/start`` is the live verb — with an idle thread the server starts a fresh
turn, with an active steerable turn it steers that turn, and either way the
reply carries the turn: the message is in the host's flow now, not parked in a
database waiting for the next idle boundary.

A JSON-RPC *error* reply means the server refused the input (an active turn
that cannot be steered — a review or a manual compaction — or an unknown
thread). Transport failures (unreachable endpoint, failed handshake, timeout)
mean the same thing to the caller: not delivered by this transport. In all
those cases the caller falls back to the durable ``codex queue`` path, whose
delivery happens at the next turn boundary; at-least-once holds across the two
paths because the pushed envelope carries the message ids either way, and a
re-delivery repeat is marked as such — the ids are the idempotency key.

This module never raises for a failed delivery: it returns a short reason for
the caller to log before falling back. Wire facts verified against codex
0.153.4: every connection starts with ``initialize`` (clientInfo) followed by
the ``initialized`` notification; requests are ``{"id", "method", "params"}``;
replies are ``{"id", "result"}`` or ``{"id", "error"}``; server notifications
and server-initiated requests interleave, so a reply is matched by id *and* by
carrying result/error. The default local endpoint is the daemon control socket
``$CODEX_HOME/app-server-control/app-server-control.sock`` (a bare
``--listen unix://`` binds exactly there).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, cast

from websockets.sync.client import ClientConnection, unix_connect
from websockets.sync.client import connect as ws_connect

LIVE_SUBMIT_TIMEOUT_SECONDS = 5.0
"""Deadline for one whole live attempt: connect, handshake, and the turn/start.

The endpoint is a local socket (or a reachable host); when it answers, the
round trips are milliseconds, so the deadline only bites against a hung
endpoint. The caller's queue fallback preserves delivery either way, and the
bound stays well under the relay's 10s heartbeat cadence.
"""

_CLOSE_TIMEOUT_SECONDS = 1.0
_MIN_REMAINING_SECONDS = 0.05
_REASON_MAX_CHARS = 200
_CLIENT_INFO: dict[str, str] = {"name": "ava-impersonation-relay", "version": "1"}
_INITIALIZE_REQUEST_ID = 1
_TURN_REQUEST_ID = 2


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

    Probed per call, not cached: the relay outlives app-server restarts, and a
    daemon that starts later must still receive live deliveries.
    """
    socket_path = default_control_socket()
    return f"unix://{socket_path}" if socket_path.exists() else None


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
    — for the caller to log before falling back to the durable queue. Never
    raises for a failed delivery.

    ``endpoint`` takes the shapes codex itself accepts: ``unix://PATH`` (bare
    ``unix://`` means the default control socket), ``ws://host:port`` or
    ``wss://host:port``. Authentication headers are not sent; the recorded
    endpoint is expected to be reachable as-is, matching the queue path.
    """
    deadline = time.monotonic() + timeout
    conn: ClientConnection | None = None
    try:
        conn = _open(endpoint, deadline)
        _initialize(conn, deadline)
        response = _request(
            conn,
            _TURN_REQUEST_ID,
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": message}]},
            deadline,
        )
        error = response.get("error")
        if error is not None:
            return _clip(f"turn/start refused ({_error_summary(error)})")
        return None
    except Exception as exc:
        # Every failure here means "not delivered by this transport" and hands
        # the message to the queue; a program error in this module degrades to
        # the same safe path instead of killing the relay.
        return _clip(f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


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
