"""Read-only health probes for pitr base backup; the root supervisor owns recovery."""

from __future__ import annotations

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = f"http://localhost:{health_port('pitr_base_backup')}/healthz"


def _probe() -> DaemonProbe:
    return probe_daemon(
        "pitr_base_backup",
        _HEALTH_URL,
        pidfile=settings.services.pitr_base_backup_pidfile,
    )
