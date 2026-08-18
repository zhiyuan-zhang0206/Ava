"""Heartbeat healthcheck — run by the gateway watchdog every 60s.

Checks whether the heartbeat daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

Idle nudging is best-effort; no catchup when the daemon dies — the next poll
naturally re-scans every idle agent.

Usage (watchdog / crontab):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.heartbeat
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.heartbeat")

# Daemon /healthz HTTP probe, same pattern as the other daemon healthchecks.
_HEALTH_URL = (
    settings.services.heartbeat_health_url or f"http://localhost:{health_port('heartbeat')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("heartbeat", _HEALTH_URL, pidfile=settings.services.heartbeat_pidfile)


def _restart_daemon() -> DaemonProbe:
    """Start heartbeat in the ava-heartbeat pane, then confirm it actually came up."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "heartbeat",
        ".venv/bin/python -m services.heartbeat.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="heartbeat-healthcheck")
    run_keepalive("heartbeat", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
