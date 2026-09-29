"""Read-only health probes for mcp daemon; the root supervisor owns recovery."""

import json
import logging
import socket

from shared.paths import mcp_daemon_shared_socket
from shared.platform_probes import unix_sockets_available

_log = logging.getLogger("services.healthchecks.mcp_daemon")

_TIMEOUT_S = 5.0


def _probe() -> bool:
    """True when the daemon answers a lock-free ping over its socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        sock.connect(str(mcp_daemon_shared_socket()))
        req = {"id": 0, "method": "ping"}
        sock.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0])
        return bool(resp.get("ok"))
    finally:
        sock.close()


def _is_alive() -> bool:
    """True when the daemon answers a ping; ANY probe failure means "not alive".

    First line of defence: where AF_UNIX is absent (a Windows agent-runner) the
    daemon cannot run at all — it binds this very socket — and is gated out of
    the ops roster (`ops.spec._gate_reason`), so the watchdog never schedules
    this check. A manual run still no-ops as alive instead of walking the
    dead -> restart path against a service that can never start (which would
    log an ERROR every minute on the win runner; measured 1,257/24h). The
    broad except below is the second line: an unforeseen probe failure still
    degrades to a verdict the watchdog can act on.
    """
    if not unix_sockets_available():
        _log.debug("[mcp-daemon healthcheck] no AF_UNIX on this host; service gated out, no-op")
        return True
    try:
        return _probe()
    except (OSError, json.JSONDecodeError):
        return False
    except Exception:
        _log.exception("[mcp-daemon healthcheck] probe raised unexpectedly; treating as dead")
        return False
