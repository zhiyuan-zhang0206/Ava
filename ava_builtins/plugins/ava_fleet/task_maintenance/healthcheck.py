"""Task-maintenance healthcheck — run by the gateway watchdog every 60s.

Checks whether the task-maintenance daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

Reminders and the stale sweep are best-effort; no catchup when the daemon dies —
the next poll re-scans every overdue / stale task.

Resolved from the plugin's ServiceSpec.healthcheck_module
(`plugins.ava_fleet.task_maintenance.healthcheck`) via `importlib` in the
watchdog, exactly like the core `services.healthchecks.*` modules — the plugin
just lives under its own namespace.

Usage (watchdog / crontab):
    * * * * * cd /path/to/Ava && .venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck")

# Daemon /healthz HTTP probe, same pattern as the other daemon healthchecks.
_HEALTH_URL = (
    settings.services.task_maintenance_health_url
    or f"http://localhost:{health_port('task_maintenance')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "task_maintenance", _HEALTH_URL, pidfile=settings.services.task_maintenance_pidfile
    )


def _restart_daemon() -> DaemonProbe:
    """Start task-maintenance in the ava-task-maintenance pane, then confirm it came up."""
    # The module path and the parents[] depth are the two things the
    # ava_builtins move silently broke (audit round 2, P1): a stale module
    # string never fails an import check at deploy time, and parents[3]
    # used to be the repo root before the file gained the ava_builtins/
    # level. find_spec smoke test in tests/plugins/ava_fleet/ pins both.
    project_root = settings.services.project_root or Path(__file__).resolve().parents[4]
    return respawn_and_verify(
        "task-maintenance",
        ".venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon",
        project_root,
        verify=_probe,
    )


def main() -> None:
    init_gateway_process()
    run_keepalive("task-maintenance", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
