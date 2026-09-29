"""Read-only health probes for gateway; the root supervisor owns recovery."""

from base.config import settings
from base.daemon.health import DaemonProbe, probe_home

_HEALTH_URL = settings.services.gateway_health_url


def _probe() -> DaemonProbe:
    """2xx from `/api/health` AND a `home` matching this unit's `$AVA_HOME`,
    ALWAYS returning a verdict — see `base.daemon.health.probe_home`."""
    return probe_home(_HEALTH_URL)
