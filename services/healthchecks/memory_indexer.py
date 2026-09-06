"""Memory indexer healthcheck — called every 60s by the watchdog daemon.

Restart if dead; no-op if alive; report-and-stop when another unit's daemon
holds the port (`shared.service_respawn.run_keepalive`). The index is not lost on restart:
the sqlite db is persistent, and on daemon start the cold-start
reconcile catches up missed fs changes during downtime — so **no
catchup dispatch is needed** in the healthcheck.

Switched to HTTP `/healthz` probe (same pattern as the 4 daemons in
#254); using pidfile would let watchdog mis-judge dead within the short
init window -> race spawn infinite loop.
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.memory_indexer")

_HEALTH_URL = (
    settings.services.memory_indexer_health_url
    or f"http://localhost:{health_port('memory_indexer')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon(
        "memory_indexer", _HEALTH_URL, pidfile=settings.services.memory_indexer_pidfile
    )


def _restart_daemon() -> DaemonProbe:
    """Start daemon in the ava-memory-indexer pane, then confirm it actually came up."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "memory-indexer",
        ".venv/bin/python -m services.memory_indexer.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="memory_indexer-healthcheck")
    run_keepalive("memory_indexer", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
