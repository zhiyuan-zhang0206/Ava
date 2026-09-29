"""Read-only health probes for agent host; the root supervisor owns recovery."""

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon

# The host is the hosted-mode agent process: it runs the agent kernel + plugins
# in-process, so its config consumption matches the `agent` profile (the
# consumption-matrix guard walks services/agent_host/ under the agent kind).
# A `runner` profile here crashes the daemon at import (settings.agent read —
# 2026-08-30 soak startup).

_HEALTH_URL = f"http://localhost:{health_port('agent_host')}/healthz"


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("agent_host", _HEALTH_URL, pidfile=settings.services.agent_host_pidfile)
