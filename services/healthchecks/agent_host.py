"""Agent-host healthcheck — run by the agent-runner watchdog every 60s.

Checks whether the hosted agent-runner daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

Every agent-runner includes this check through its ServiceSpec healthcheck.

Imports nothing from `services.agent_host`: the host module pulls the whole
LangGraph/agent-kernel chain, and a healthcheck the watchdog runs every 60s
must stay a cheap probe.

Usage (watchdog / crontab):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.agent_host
"""

import logging
from pathlib import Path

from shared.cluster.derive import runner_db_url_projection
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


def _agent_host_env() -> dict[str, str]:
    """The agent-profile environment for every hosted-agent-host launch."""
    return {
        "AVA_PROCESS_PROFILE": _HOST_PROCESS_PROFILE,
        "AVA_DB_URL": runner_db_url_projection(settings.data_plane.db_url),
    }


def _restart_daemon() -> DaemonProbe:
    """Start the host in the ava-agent-host pane, then confirm it came up."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "agent-host",
        ".venv/bin/python -m services.agent_host.daemon",
        project_root,
        extra_env=_agent_host_env(),
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="agent_host-healthcheck")
    run_keepalive("agent-host", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
