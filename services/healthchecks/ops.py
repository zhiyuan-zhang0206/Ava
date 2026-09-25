"""Read-only health probes for ops; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = f"http://localhost:{health_port('ops')}/healthz"


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`.

    The ops healthz binds 0.0.0.0 (the gateway dials it over the private network),
    so the identity check matters more here than elsewhere: without it any process
    that grabbed this port first — including another unit on the same box — reads
    as a healthy ops server."""
    return probe_daemon("ops", _HEALTH_URL, pidfile=settings.services.ops_pidfile)
