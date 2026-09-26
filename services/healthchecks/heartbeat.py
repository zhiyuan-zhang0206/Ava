"""Read-only health probes for heartbeat; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

# Daemon /healthz HTTP probe, same pattern as the other daemon healthchecks.
_HEALTH_URL = (
    settings.services.heartbeat_health_url or f"http://localhost:{health_port('heartbeat')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("heartbeat", _HEALTH_URL, pidfile=settings.services.heartbeat_pidfile)
