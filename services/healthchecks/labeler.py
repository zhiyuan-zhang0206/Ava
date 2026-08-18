"""Labeler healthcheck — run by the gateway watchdog every 60s.

Checks whether the labeler daemon is alive:
- alive -> no-op
- dead -> restart daemon
- port held by another unit's daemon -> report at ERROR, no respawn
  (`shared.service_respawn.run_keepalive` holds the shared policy)

Label generation is best-effort; no re-delivery when the daemon dies —
the next poll will naturally process the backlog of unlabeled agents.

Usage (watchdog):
    * * * * * cd /path/to/Ava && .venv/bin/python -m services.healthchecks.labeler
"""

import logging
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.labeler")

# Switched to daemon /healthz HTTP probe, same pattern as gateway healthcheck (#251).
_HEALTH_URL = (
    settings.services.labeler_health_url or f"http://localhost:{health_port('labeler')}/healthz"
)


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `shared.daemon_health.probe_daemon`."""
    return probe_daemon("labeler", _HEALTH_URL, pidfile=settings.services.labeler_pidfile)


def _restart_daemon() -> DaemonProbe:
    """Start labeler in the ava-labeler pane, then confirm it actually came up."""
    # Deliberately NO AVA_PROCESS_PROFILE marker: the gateway profile's env-authority
    # pass drops agent-runner cluster aliases (DEEPSEEK_API_KEY among them) from
    # os.environ, which leaves settings.lm.deepseek_api_key=None and every label
    # generation failing (#1128, 2026-08-10). No marker = full Settings — the same
    # choice the initial `ava start` spawn now makes via the labeler's
    # `no_profile_marker=True` spec (ops/spec.py, task #1230), so respawn and
    # first start agree.
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "labeler",
        ".venv/bin/python -m services.labeler.daemon",
        project_root,
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="labeler-healthcheck")
    run_keepalive("labeler", _log, probe=_probe, respawn=_restart_daemon)


if __name__ == "__main__":
    main()
