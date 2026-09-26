"""Read-only health probes for browser mcp; the root supervisor owns recovery."""

import json
import logging
import socket

from services.browser.protocol import Request, Response
from shared.paths import chrome_mcp_socket

_log = logging.getLogger("services.healthchecks.browser_mcp")

_TIMEOUT_S = 5.0


def _probe() -> bool:
    """True when the daemon answers a lock-free ping over its socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        sock.connect(str(chrome_mcp_socket()))
        req: Request = {"id": 0, "method": "ping"}
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

    A healthcheck's contract with the watchdog is a bool, not an exception. The
    narrow `(OSError, json.JSONDecodeError)` this used to catch let
    `socket.AF_UNIX`'s AttributeError through on Windows, and the watchdog logged
    a multi-KB "healthcheck browser-mcp raised" traceback every 60s while never
    deciding alive-or-dead — no restart was ever attempted, and the round's
    remaining checks ran only because the watchdog isolates each one.

    The service is now gated out where AF_UNIX is absent (`ops.spec._gate_reason`),
    so that specific failure no longer reaches here. This is the second line of
    defence: an unforeseen probe failure degrades to "dead" — a decision the
    caller can act on — instead of to no answer at all. Unexpected types still
    log a traceback, so nothing is silently swallowed.
    """
    try:
        return _probe()
    except (OSError, json.JSONDecodeError):
        return False
    except Exception:
        _log.exception("[browser-mcp healthcheck] probe raised unexpectedly; treating as dead")
        return False
