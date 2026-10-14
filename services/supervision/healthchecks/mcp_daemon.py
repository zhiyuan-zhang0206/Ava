"""Read-only health probes for mcp daemon; the root supervisor owns recovery."""

import json
import logging
import socket
from pathlib import Path

from base.paths import mcp_daemon_shared_socket

_log = logging.getLogger("services.supervision.healthchecks.mcp_daemon")

_TIMEOUT_S = 5.0


def mcp_daemon_probe(home: Path) -> bool:
    """True when the daemon of `home` answers a lock-free ping over its socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        sock.connect(str(mcp_daemon_shared_socket(home)))
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


def is_alive(home: Path) -> bool:
    """True when the daemon of `home` answers a ping; ANY probe failure means "not alive".

    The broad except below makes an unforeseen probe failure degrade to a verdict
    the watchdog can act on.
    """
    try:
        return mcp_daemon_probe(home)
    except (OSError, json.JSONDecodeError):
        return False
    except Exception:
        _log.exception("[mcp-daemon healthcheck] probe raised unexpectedly; treating as dead")
        return False
