"""Delivery watchdog healthcheck — run by the gateway watchdog every 60s.

Checks whether the delivery watchdog daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

Usage (watchdog / crontab):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.delivery_watchdog
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.delivery_watchdog")

_HEALTH_URL = (
    settings.services.delivery_watchdog_health_url
    or f"http://localhost:{health_port('delivery_watchdog')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "delivery_watchdog", _HEALTH_URL, pidfile=settings.services.delivery_watchdog_pidfile
    )


def _restart_daemon() -> DaemonProbe:
    """Start delivery watchdog in the ava-delivery-watchdog pane, then confirm it came up."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "delivery-watchdog",
        ".venv/bin/python -m services.delivery_watchdog.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="delivery_watchdog-healthcheck")
    run_keepalive("delivery-watchdog", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
