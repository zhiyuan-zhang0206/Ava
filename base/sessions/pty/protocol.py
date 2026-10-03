"""Wire format of the pty-sessions service: one JSON object per line.

A request is ``{"id": <int>, "method": <str>, ...fields}``; the response echoes
the ``id`` so the shared ownership probe (``services.healthchecks.owned_service``)
and every client can pair them: ``{"id", "ok", "code", "data", "error"}``.
Codes: 0 success, 1 operational error, 2 bad request, 3 no such session.

Stdlib only: the client side runs inside agent processes.
"""

from __future__ import annotations

import json
import socket
from typing import Any, cast

# A request line this long is garbage, not a session op. `new` carries a full
# environment and is the largest legitimate request.
MAX_REQUEST_BYTES = 1 << 20

OK = 0
ERROR = 1
BAD_REQUEST = 2
NO_SUCH_SESSION = 3


def ok(req_id: object, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "code": OK, "data": data, "error": None}


def err(req_id: object, code: int, message: str) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "code": code, "data": None, "error": message}


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message) + "\n").encode("utf-8")


def read_line(conn: socket.socket, limit: int = MAX_REQUEST_BYTES) -> bytes | None:
    """The first line on `conn` without its newline; None when the peer closed first.

    Raises ValueError when the line outgrows `limit`.
    """
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf += chunk
        if b"\n" in buf:
            return buf.split(b"\n", 1)[0]
        if len(buf) > limit:
            raise ValueError(f"line exceeds {limit} bytes")


def decode_object(line: bytes) -> dict[str, Any]:
    """Parse one line into a JSON object (ValueError/TypeError on anything else)."""
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("message is not a JSON object")
    return cast("dict[str, Any]", value)
