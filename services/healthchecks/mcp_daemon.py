"""ava-mcp-daemon healthcheck — called every 60s by the watchdog.

Probe the shared MCP daemon over its Unix socket with a lock-free `ping`
(connect + reply proves the accept/read loop is alive). On death, respawn the
daemon via `shared.service_respawn.respawn_and_verify` (same pattern as the
browser / browser-mcp healthchecks) and report success only once the probe
confirms the daemon answers again.
"""

import json
import logging
import socket
import sys
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe
from shared.log import init_gateway_process
from shared.paths import mcp_daemon_shared_socket
from shared.platform_probes import unix_sockets_available
from shared.service_respawn import respawn_and_verify

_log = logging.getLogger("services.healthchecks.mcp_daemon")

_TIMEOUT_S = 5.0
_CMD = ".venv/bin/python -m ava._mcps_daemon"


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


def _restart_daemon() -> bool:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent

    def _verify() -> DaemonProbe:
        if _is_alive():
            return DaemonProbe.up("socket ping ok")
        return DaemonProbe.down("socket ping failed")

    # Verify by the probe, not by the launch: the respawned daemon refuses to
    # start over a live socket (Task #1142), so a launch can be accepted while
    # the previous instance is still serving — which is fine, and the probe
    # says so.
    probe = respawn_and_verify(
        "mcp-daemon",
        _CMD,
        project_root,
        verify=_verify,
        extra_env={"AVA_PROCESS_PROFILE": "runner"},
    )
    return probe.alive


def main() -> None:
    init_gateway_process(name="mcp-daemon-healthcheck")
    if _is_alive():
        _log.debug("[mcp-daemon healthcheck] alive, no-op")
        return
    _log.info("[mcp-daemon healthcheck] dead, restarting...")
    if _restart_daemon():
        _log.info("[mcp-daemon healthcheck] daemon restarted")
    else:
        _log.error("[mcp-daemon healthcheck] restart FAILED — manual intervention needed")
        sys.exit(1)


if __name__ == "__main__":
    main()
