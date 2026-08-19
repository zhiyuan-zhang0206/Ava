"""Page server supervisor healthcheck — called every 60s by the
agent-runner watchdog.

Checks whether the page_server daemon is alive (identity-verified /healthz,
not merely a 200); dead -> respawn it. There is no stand-in: page servers
are cheap to start, so a round without the supervisor at worst leaves
stale/closed pages running until the daemon is back — nothing blocks on it.

Usage (standalone, e.g. a manual operator run):
    cd /path/to/Ava && .venv/bin/python -m services.healthchecks.page_server
"""

from __future__ import annotations

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.page_server")

_HEALTH_URL = (
    settings.services.page_server_health_url
    or f"http://localhost:{health_port('page_server')}/healthz"
)


def _probe() -> DaemonProbe:
    return probe_daemon("page_server", _HEALTH_URL, pidfile=settings.services.page_server_pidfile)


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    # The respawn session name MUST match ServiceSpec.session ("page-server",
    # kebab-case) — see Task #1291: the module name ("page_server") differs and
    # a respawn under it strands the session where the CLI cannot see or kill it.
    return respawn_and_verify(
        "page-server",
        ".venv/bin/python -m services.page_server.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "runner"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="page_server-healthcheck")
    run_keepalive(
        "page_server",
        _log,
        probe=_probe,
        respawn=_restart_daemon,
    )


if __name__ == "__main__":
    main()
