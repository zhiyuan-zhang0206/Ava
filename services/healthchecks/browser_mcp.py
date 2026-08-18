"""ava-browser-mcp healthcheck — called every 60s by the watchdog.

Probe the shared chrome MCP service over its Unix socket with a lock-free `ping`
(connect + reply proves the accept/read loop is alive). `ping` deliberately does
NOT round-trip to the upstream or take the daemon's serial lock — a slow browser
op can hold that lock past the probe timeout, and probing through it would
false-kill a busy-but-healthy daemon. A dead upstream is surfaced the other way:
the daemon exits, its socket vanishes, and the connect below fails -> restart. On
death, respawn the daemon in the ava-browser-mcp session via
`shared.service_respawn.respawn_service` (same pattern as the browser / milvus
healthchecks).
"""

import json
import logging
import socket
import sys
from pathlib import Path

from services.browser.protocol import Request, Response
from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import chrome_mcp_socket
from shared.service_respawn import respawn_service

_log = logging.getLogger("services.healthchecks.browser_mcp")

_TIMEOUT_S = 5.0
_CMD = ".venv/bin/python -m services.browser.mcp_daemon"


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


def _restart_daemon() -> bool:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_service(
        "browser-mcp", _CMD, project_root, extra_env={"AVA_PROCESS_PROFILE": "runner"}
    )


def main() -> None:
    init_gateway_process(name="browser-mcp-healthcheck")
    if _is_alive():
        _log.debug("[browser-mcp healthcheck] alive, no-op")
        return
    _log.info("[browser-mcp healthcheck] dead, restarting...")
    if _restart_daemon():
        _log.info("[browser-mcp healthcheck] daemon restarted")
    else:
        _log.error("[browser-mcp healthcheck] restart FAILED — manual intervention needed")
        sys.exit(1)


if __name__ == "__main__":
    main()
