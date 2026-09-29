"""Read-only health probes for labeler; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

# Switched to daemon /healthz HTTP probe, same pattern as gateway healthcheck (#251).
_HEALTH_URL = (
    settings.services.labeler_health_url or f"http://localhost:{health_port('labeler')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("labeler", _HEALTH_URL, pidfile=settings.services.labeler_pidfile)
