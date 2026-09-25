"""Read-only health probes for page server; the root supervisor owns recovery."""

from __future__ import annotations

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = (
    settings.services.page_server_health_url
    or f"http://localhost:{health_port('page_server')}/healthz"
)


def _probe() -> DaemonProbe:
    return probe_daemon("page_server", _HEALTH_URL, pidfile=settings.services.page_server_pidfile)
