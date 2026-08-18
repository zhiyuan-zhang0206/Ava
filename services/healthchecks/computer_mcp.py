"""ava-computer-mcp healthcheck — called every 60s by the watchdog.

Probe the shared computer-use service over its Unix socket with a lock-free
`ping` (connect + reply proves the accept/read loop is alive). `ping` does NOT
round-trip to the permissions helper or take the daemon's action lock — a slow
desktop action can hold that lock past the probe timeout, and probing through
it would false-kill a busy-but-healthy daemon. On death, respawn the daemon in
the ava-computer-mcp session via `shared.service_respawn.respawn_service`
(same pattern as the browser / browser-mcp healthchecks).
"""

import json
import logging
import socket
from pathlib import Path

from services.computer.protocol import Request, Response
from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import computer_mcp_socket
from shared.service_respawn import respawn_service

_log = logging.getLogger("services.healthchecks.computer_mcp")

_TIMEOUT_S = 5.0
_CMD = ".venv/bin/python -m services.computer.mcp_daemon"


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


def _restart() -> bool:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_service(
        "computer-mcp", _CMD, project_root, extra_env={"AVA_PROCESS_PROFILE": "runner"}
    )


def main() -> None:
    init_gateway_process(name="computer-mcp-healthcheck")
    if _is_alive():
        _log.info("[computer-mcp healthcheck] alive, no-op")
        return
    _log.info("[computer-mcp healthcheck] dead, restarting...")
    if _restart():
        _log.info("[computer-mcp healthcheck] daemon restarted")


if __name__ == "__main__":
    main()
