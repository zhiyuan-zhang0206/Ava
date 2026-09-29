"""Read-only health probes for pg backup; the root supervisor owns recovery."""

from __future__ import annotations

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = (
    settings.services.pg_backup_health_url or f"http://localhost:{health_port('pg_backup')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("pg_backup", _HEALTH_URL, pidfile=settings.services.pg_backup_pidfile)
