"""Restart dispatcher healthcheck — called every 60s by the agent-runner watchdog.

Checks whether the restarter daemon is alive:
- alive -> no-op (daemon is dispatching normally)
- dead -> respawn the daemon; if it will not come up, dispatch this round's
  restarts in its place (`_standin_dispatch`)
- port held by another unit's daemon -> no respawn at all (nothing this unit can
  do frees that port), report at ERROR, and dispatch in its place

"Alive" means an identity-verified `/healthz` (`shared.daemon_health.probe_daemon`),
not merely a 200: this healthcheck was the one a leaked test daemon on prod's
default port kept green for 98 minutes while the real restarter was dead.

**Ordering is load-bearing: the respawn runs BEFORE any DB work, never after.**
The only way to reach the dead branch is that the daemon is already gone, so
anything placed ahead of the respawn becomes a precondition for recovery. A
DB-reading catch-up used to sit there, and a dead DB made this healthcheck raise
out of `main()` — the watchdog isolates each check, so the round survived and
`_restart_daemon` was simply never reached. That is the probe contract's failure
one layer out ([[healthchecks/probe-contract.ava.okf.md]]): "no restart is ever
attempted", quietly. A DB outage and a daemon crash are independent events;
nothing about the first may gate recovery from the second.

Nor is the catch-up needed on the success path: the daemon's own
`RespawnController` sweeps this host's `restarting` rows on its first tick (~1s
after it comes up), with a machine scope and a gateway-health gate the healthcheck
lacked. What survives is the case where the respawn *cannot* succeed — see
`_standin_dispatch`.

The probe/respawn/report policy itself lives in
`shared.service_respawn.run_keepalive`, shared with every other daemon healthcheck;
the stand-in rides it as the `on_unrevivable` fallback, which by construction runs
only after a respawn attempt (or in place of one that would be futile).

Usage (standalone, e.g. a manual operator run):
    cd /path/to/Ava && .venv/bin/python -m services.healthchecks.restarter
"""

import logging
from pathlib import Path

import shared.db
from ops.controllers.respawn import RespawnController
from shared.config import settings
from shared.daemon_health import DaemonProbe, health_port, probe_daemon
from shared.log import init_gateway_process
from shared.service_respawn import respawn_and_verify, run_keepalive

_log = logging.getLogger("services.healthchecks.restarter")

# Switched to daemon /healthz HTTP probe, same pattern as gateway
# healthcheck (#251). See shared/daemon_health.py docstring.
_HEALTH_URL = (
    settings.services.restarter_health_url or f"http://localhost:{health_port('restarter')}/healthz"
)

# Pool timeout for the stand-in dispatch — deliberately not psycopg_pool's 30s
# default. The stand-in runs inside the watchdog's SEQUENTIAL tick (`_run_check` is
# awaited per check), so a half-dead DB here delays every check queued behind it;
# 5s matches the `connect_timeout` on `shared.db`'s own connections.
_STANDIN_POOL_TIMEOUT_S = 5.0


def _probe() -> DaemonProbe:
    """Identity-verified liveness: a 200 from a process that is not this unit's
    restarter is judged dead. See `shared.daemon_health.probe_daemon` — this
    healthcheck is the one the 2026-07-24 impostor-on-8102 outage fooled."""
    return probe_daemon("restarter", _HEALTH_URL, pidfile=settings.services.restarter_pidfile)


def _standin_dispatch() -> None:
    """Dispatch this host's 'restarting' rows once, standing in for a daemon that
    could not be revived.

    Reached only when the round will have no live daemon: the respawn failed to
    verify, or the verdict was terminal so no respawn was attempted. The daemon is
    what dispatches restarts, so while it stays down every 'restarting' row on this
    host is frozen — the 98-minute shape of the 2026-07-24 outage, where an impostor
    held the port and no daemon could ever bind it while the DB and gateway were
    perfectly healthy. That impostor case is now the terminal verdict, where "until a
    human fixes it" is not a figure of speech, so this is the only thing keeping
    restarts flowing at the watchdog's 60s cadence.

    It delegates to the daemon's own ``RespawnController`` rather than
    re-implementing the dispatch. The hand-rolled copy this replaces was neither
    machine-scoped nor gateway-health-gated: it respawned OTHER machines' agents on
    this host, which trips the boot placement gate (``agent/_starting.py``) and burns
    the restart — the CAS has already moved the row off 'restarting', so the rejected
    boot leaves a corpse and the restart request itself is gone. Recovery then rests
    on CrashResurrect, which only claims rows holding a pending inbound in its
    workload allowlist; a restart is not in that allowlist.

    Total by contract — it never raises. It runs AFTER the respawn verdict so a dead
    DB can neither block the respawn nor mask the non-zero exit that reports it
    failed; `run_keepalive` calls its `on_unrevivable` hook only there, so that
    ordering no longer depends on this module keeping its `main()` in the right shape.
    """
    try:
        pool = shared.db.pool(timeout=_STANDIN_POOL_TIMEOUT_S)
    except Exception:
        _log.exception("[restarter healthcheck] stand-in dispatch: no DB pool, skipping this round")
        return
    try:
        result = RespawnController(pool).reconcile("agent-runner")
        _log.info("[restarter healthcheck] stand-in dispatch ran (acted=%s)", result.acted)
    except Exception:
        _log.exception("[restarter healthcheck] stand-in dispatch failed; retry next round")
    finally:
        pool.close()


def _restart_daemon() -> DaemonProbe:
    """Start restarter in the ava-restarter pane, then confirm it actually came up.

    Reports what the daemon proves, not what the spawn accepted — see
    ``shared.service_respawn.respawn_and_verify``."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_and_verify(
        "restarter",
        ".venv/bin/python -m services.restarter.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "runner"},
        verify=_probe,
    )


def main() -> None:
    init_gateway_process(name="restarter-healthcheck")
    # The respawn runs before any DB work, and the stand-in only after it — see the
    # module docstring. `run_keepalive` owns that ordering for every healthcheck: the
    # `on_unrevivable` hook is never called ahead of a respawn attempt.
    run_keepalive(
        "restarter",
        _log,
        probe=_probe,
        respawn=_restart_daemon,
        on_unrevivable=_standin_dispatch,
    )


if __name__ == "__main__":
    main()
