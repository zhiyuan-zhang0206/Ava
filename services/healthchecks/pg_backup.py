"""Keep the Postgres backup scheduler alive without running its dump here.

The daemon decides whether its last success is fresh enough for `/healthz`.
This healthcheck only applies the common identity-verified probe and respawn
policy, keeping the watchdog round bounded even while a dump runs elsewhere.
"""

from __future__ import annotations

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.pg_backup")

_HEALTH_URL = (
    settings.services.pg_backup_health_url or f"http://localhost:{health_port('pg_backup')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("pg_backup", _HEALTH_URL, pidfile=settings.services.pg_backup_pidfile)


def _restart_daemon() -> DaemonProbe:
    """Start the pg-backup scheduler, then confirm it owns its health endpoint."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "pg-backup",
        ".venv/bin/python -m services.backup_scheduler.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="pg-backup-healthcheck")
    run_keepalive("pg-backup", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
