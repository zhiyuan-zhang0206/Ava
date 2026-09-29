"""Read-only task-maintenance liveness probe; root owns process recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = (
    settings.services.task_maintenance_health_url
    or f"http://localhost:{health_port('task_maintenance')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "task_maintenance", _HEALTH_URL, pidfile=settings.services.task_maintenance_pidfile
    )
