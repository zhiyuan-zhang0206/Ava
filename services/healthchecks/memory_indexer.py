"""Read-only health probes for memory indexer; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = (
    settings.services.memory_indexer_health_url
    or f"http://localhost:{health_port('memory_indexer')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "memory_indexer", _HEALTH_URL, pidfile=settings.services.memory_indexer_pidfile
    )
