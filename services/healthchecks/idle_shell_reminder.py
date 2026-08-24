"""Keepalive for the gateway idle-shell-reminder daemon."""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.idle_shell_reminder")

_HEALTH_URL = f"http://localhost:{health_port('idle_shell_reminder')}/healthz"


def _probe() -> DaemonProbe:
    """Identity-verified liveness for the daemon's ticking work loop."""
    return probe_daemon(
        "idle_shell_reminder",
        _HEALTH_URL,
        pidfile=settings.services.idle_shell_reminder_pidfile,
    )


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "idle-shell-reminder",
        ".venv/bin/python -m services.idle_shell_reminder.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="idle_shell_reminder-healthcheck")
    run_keepalive("idle-shell-reminder", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
