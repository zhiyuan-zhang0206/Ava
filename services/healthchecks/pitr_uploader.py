"""Read-only health probes for pitr uploader; the root supervisor owns recovery."""

from __future__ import annotations

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

_HEALTH_URL = f"http://localhost:{health_port('pitr_uploader')}/healthz"


def _probe() -> DaemonProbe:
    return probe_daemon(
        "pitr_uploader", _HEALTH_URL, pidfile=settings.services.pitr_uploader_pidfile
    )
