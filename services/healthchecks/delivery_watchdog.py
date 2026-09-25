"""Read-only health probes for delivery watchdog; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = (
    settings.services.delivery_watchdog_health_url
    or f"http://localhost:{health_port('delivery_watchdog')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "delivery_watchdog", _HEALTH_URL, pidfile=settings.services.delivery_watchdog_pidfile
    )
