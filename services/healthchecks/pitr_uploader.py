"""Keepalive healthcheck for the gateway-owned PITR uploader."""

from __future__ import annotations

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.pitr_uploader")

_HEALTH_URL = f"http://localhost:{health_port('pitr_uploader')}/healthz"


def _probe() -> DaemonProbe:
    return probe_daemon(
        "pitr_uploader", _HEALTH_URL, pidfile=settings.services.pitr_uploader_pidfile
    )


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "pitr-uploader",
        ".venv/bin/python -m services.pitr.uploader_daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="pitr-uploader-healthcheck")
    run_keepalive("pitr-uploader", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
