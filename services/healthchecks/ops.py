"""ava-ops healthcheck — called every 60s by the watchdog daemon on agent-runners.

Checks whether the ops-server daemon is alive via its /healthz HTTP port:
- alive       -> no-op
- dead        -> respawn the daemon in its session
- port taken  -> report at ERROR and stop; no respawn can free it

The three-way probe policy is shared
(`shared.service_respawn.run_keepalive`).

This healthcheck and the restarter's are the pair that looped on the `win` box:
WSL2 forwards the Linux unit's ports onto Windows' localhost, so the Windows unit's
probes of `:8102` / `:8106` reach the WSL unit's daemons. The identity check caught
it correctly; the respawn behind it could not, and had no business trying.
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.ops")

_HEALTH_URL = f"http://localhost:{health_port('ops')}/healthz"


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`.

    The ops healthz binds 0.0.0.0 (the gateway dials it over the private network),
    so the identity check matters more here than elsewhere: without it any process
    that grabbed this port first — including another unit on the same box — reads
    as a healthy ops server."""
    return probe_daemon("ops", _HEALTH_URL, pidfile=settings.services.ops_pidfile)


def _restart_daemon() -> DaemonProbe:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "ops",
        ".venv/bin/python -m services.agent_ops.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "runner"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="ops-healthcheck")
    run_keepalive("ops", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
