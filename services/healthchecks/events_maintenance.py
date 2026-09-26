"""Read-only health probes for events maintenance; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

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
