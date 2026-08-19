"""IM Bridge healthcheck — run by the gateway watchdog every 60s.

Checks whether the im_bridge daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn

Usage (watchdog):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.im_bridge
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.im_bridge")

_HEALTH_URL = (
    settings.services.im_bridge_health_url or f"http://localhost:{health_port('im_bridge')}/healthz"
)


def _probe() -> DaemonProbe:
    return probe_daemon("im_bridge", _HEALTH_URL, pidfile=settings.services.im_bridge_pidfile)


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    # The respawn session name MUST match ServiceSpec.session ("im-bridge",
    # kebab-case) — respawn_service composes the session name from it, and
    # `ava status` / `ava stop` / `ava restart` look up the same name. The
    # module name ("im_bridge") differs from the session name for this service,
    # and a respawn under the module name strands the session where the CLI
    # cannot see or kill it (Task #1291).
    return respawn_and_verify(
        "im-bridge",
        ".venv/bin/python -m services.im_bridge.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="im_bridge-healthcheck")
    run_keepalive("im_bridge", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
