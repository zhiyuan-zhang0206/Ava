"""Events-maintenance healthcheck — run by the gateway watchdog every 60s.

Checks whether the events-maintenance daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

The rollup is idempotent and self-catching-up; no catchup logic is needed when
the daemon dies — the next poll re-rolls the whole tail (start day is anchored to
the last rolled day, end is always yesterday), so a downtime gap is recovered on
the first run after respawn.

Usage (watchdog / crontab):
    * * * * * cd /path/to/ava && .venv/bin/python -m services.healthchecks.events_maintenance
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.events_maintenance")

# Daemon /healthz HTTP probe, same pattern as the other daemon healthchecks.
_HEALTH_URL = (
    settings.services.events_maintenance_health_url
    or f"http://localhost:{health_port('events_maintenance')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "events_maintenance",
        _HEALTH_URL,
        pidfile=settings.services.events_maintenance_pidfile,
    )


def _restart_daemon() -> DaemonProbe:
    """Start events-maintenance in the ava-events-maintenance pane, then confirm
    it actually came up."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "events-maintenance",
        ".venv/bin/python -m services.events_maintenance.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="events_maintenance-healthcheck")
    run_keepalive("events-maintenance", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
