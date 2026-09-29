"""Read-only health probes for computer mcp; the root supervisor owns recovery."""

import json
import logging
import socket

from services.computer.protocol import Request, Response
from shared.paths import computer_mcp_socket

_log = logging.getLogger("services.healthchecks.computer_mcp")

_TIMEOUT_S = 5.0


def _probe() -> bool:
    """True when the daemon answers a lock-free ping over its socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        sock.connect(str(computer_mcp_socket()))
        req: Request = {"id": 0, "method": "ping", "agent_id": None}
        sock.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buf += chunk
        resp: Response = json.loads(buf.split(b"\n", 1)[0])
        return bool(resp.get("ok"))
    finally:
        sock.close()


def _is_alive() -> bool:
    """True when the daemon answers a ping; ANY probe failure means "not alive".

    Same contract as the browser-mcp healthcheck: a bool, not an exception, so
    the watchdog can act on it. The service is gated out where AF_UNIX is
    absent (`ops.spec._gate_reason`), so that failure class never reaches here;
    an unforeseen probe failure degrades to "dead" — a decision the caller can
    act on — instead of to no answer at all.
    """
    try:
        return _probe()
    except (OSError, json.JSONDecodeError):
        return False
    except Exception:
        _log.exception("[computer-mcp healthcheck] probe raised unexpectedly; treating as dead")
        return False
