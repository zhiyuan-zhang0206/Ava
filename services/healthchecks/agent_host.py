"""Agent-host healthcheck — run by the agent-runner watchdog every 60s.

Checks whether the hosted agent-runner daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

The watchdog only asks this on a cluster where `AVA_RUNNER_MODE` is `hosted`:
the keepalive roster is derived from `ServiceSpec.healthcheck_module`, and the
spec is gated out of the roster in process mode, so a process-mode cluster never
reaches this module at all. The daemon refuses to start in process mode anyway,
which is what keeps a respawn from resurrecting a host on a cluster that changed
its mind mid-round.

Imports nothing from `services.agent_host`: the host module pulls the whole
LangGraph/agent-kernel chain, and a healthcheck the watchdog runs every 60s
must stay a cheap probe.

Usage (watchdog / crontab):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.agent_host
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.agent_host")

# The host is the hosted-mode agent process: it runs the agent kernel + plugins
# in-process, so its config consumption matches the `agent` profile (the
# consumption-matrix guard walks services/agent_host/ under the agent kind).
# A `runner` profile here crashes the daemon at import (settings.agent read —
# 2026-08-30 soak startup).
_HOST_PROCESS_PROFILE = "agent"

_HEALTH_URL = f"http://localhost:{health_port('agent_host')}/healthz"


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("agent_host", _HEALTH_URL, pidfile=settings.services.agent_host_pidfile)


def _restart_daemon() -> DaemonProbe:
    """Start the host in the ava-agent-host pane, then confirm it came up."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "agent-host",
        ".venv/bin/python -m services.agent_host.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": _HOST_PROCESS_PROFILE},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="agent_host-healthcheck")
    run_keepalive("agent-host", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
