"""Read-only health probes for pg backup; the root supervisor owns recovery."""

from __future__ import annotations

from base.config import settings
from base.daemon.health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = (
    settings.services.pg_backup_health_url or f"http://localhost:{health_port('pg_backup')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `base.daemon.health.probe_daemon`."""
    return probe_daemon("pg_backup", _HEALTH_URL, pidfile=settings.services.pg_backup_pidfile)
