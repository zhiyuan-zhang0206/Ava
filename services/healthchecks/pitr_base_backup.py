"""Keepalive healthcheck for the gateway-owned base candidate scheduler."""

from __future__ import annotations

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.pitr_base_backup")
_HEALTH_URL = f"http://localhost:{health_port('pitr_base_backup')}/healthz"


def _probe() -> DaemonProbe:
    return probe_daemon(
        "pitr_base_backup",
        _HEALTH_URL,
        pidfile=settings.services.pitr_base_backup_pidfile,
    )


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "pitr-base-candidate",
        ".venv/bin/python -m services.pitr.base_scheduler_daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="pitr-base-candidate-healthcheck")
    run_keepalive("pitr-base-candidate", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
