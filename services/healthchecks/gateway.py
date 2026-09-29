"""Read-only health probes for gateway; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, probe_home

_HEALTH_URL = settings.services.gateway_health_url


def _probe() -> DaemonProbe:
    """2xx from `/api/health` AND a `home` matching this unit's `$AVA_HOME`,
    ALWAYS returning a verdict — see `shared.daemon_health.probe_home`."""
    return probe_home(_HEALTH_URL)
