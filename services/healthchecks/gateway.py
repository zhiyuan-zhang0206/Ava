"""Gateway healthcheck — called every 60s by the watchdog daemon.

Checks whether the gateway HTTP server is alive:
- HTTP `/api/health` returns 2xx AND reports this unit's `$AVA_HOME` -> no-op
  (probe URL = `settings.services.gateway_health_url`)
- unreachable -> respawn in the `ava-gateway` session, then re-probe to confirm
- answered by another cluster's gateway -> report at ERROR and stop; a respawn
  cannot free a port this unit has no way to release
  (`shared.service_respawn.run_keepalive` holds the shared three-way policy)

The gateway runs uvicorn (reload configurable); supervisor / worker are
different processes, so pidfile cannot reliably reflect "is ASGI up" —
only HTTP reachability signals that the app handles requests. This
module probes via HTTP rather than pidfile (same pattern as the
frontend healthcheck), and identifies the responder by `$AVA_HOME`
rather than by pid for the same reason.

The probe itself is `shared.daemon_health.probe_home` — the home-only sibling of
`probe_daemon`, which is where the reasoning for dropping the `name`/`pid` arms
lives. It is shared rather than local because `ava status` and
`ava cluster health-probe` must ask the *same* question this watchdog asks; a
second copy here is how the operator surface came to read green against an
occupant in the first place.

Usage (watchdog daemon):
    every 60s `services.watchdog.daemon` spawns a thread to call this main().
"""

import logging
import os
import signal
from pathlib import Path

from shared.config import settings
from shared.daemon_health import DaemonProbe, probe_home
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.gateway")

_HEALTH_URL = settings.services.gateway_health_url


def _probe() -> DaemonProbe:
    """2xx from `/api/health` AND a `home` matching this unit's `$AVA_HOME`,
    ALWAYS returning a verdict — see `shared.daemon_health.probe_home`."""
    return probe_home(_HEALTH_URL)


def _thread_dump() -> None:
    """Best-effort SIGUSR1 to the gateway so its faulthandler registration
    (gateway/app.py main) writes a thread dump to the pane log before we kill
    it. A frozen gateway is otherwise a black box — the 2026-08-03 freezes left
    nothing between the last log line and the kill, which is why the root cause
    (a sync embed call blocking the event loop) took 13 restarts to pin down."""
    pidfile = settings.services.gateway_pidfile
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGUSR1)
        _log.info("sent SIGUSR1 to gateway pid %s for a thread dump", pid)
    except Exception as exc:
        _log.debug("SIGUSR1 thread dump skipped (stale pidfile?): %r", exc)


def _restart() -> DaemonProbe:
    """Start the gateway in its ava-gateway session, then confirm it came up.

    Goes through the shared service-respawn helper rather than
    ``subprocess.Popen(start_new_session=True)`` detach — see
    ``shared/service_respawn.py`` docstring.
    """
    # The probe failed; if the process is alive-but-frozen, capture its stack
    # before the respawn kills it.
    _thread_dump()
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "gateway",
        ".venv/bin/python -m gateway",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="gateway-healthcheck")
    run_keepalive("gateway", _log, probe=_probe, respawn=_restart)


if __name__ == "__main__":
    main()
