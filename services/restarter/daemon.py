"""Restart dispatcher daemon — a thin main over the ops respawn controller.

Every second it runs one `ops.controllers.respawn.RespawnController` pass: respawn
this host's `restarting` agents (gateway-health-gated) and reap orphaned rows whose
process is gone. Decoupled from the gateway's HTTP control plane (it never calls
`POST /api/agents`), but NOT from the gateway itself: respawns are
gateway-health-gated — with the gateway unreachable, every respawned agent would
crash during boot, so the controller defers instead. The reconcile+respawn+reap
logic lives in the controller; this daemon owns only the process scaffolding
(pidfile, /healthz + liveness, the DB pool, the poll loop and its schema-drift
exit policy).

Usage:
    .venv/bin/python -m services.restarter.daemon

Kept alive via `services/healthchecks/restarter.py` from the agent-runner watchdog.
"""

import asyncio
import logging
import os
import sys
import time

import psycopg
from psycopg_pool import ConnectionPool

import shared.db
from ops.controllers.hibernate import HibernateController
from ops.controllers.respawn import RespawnController
from ops.controllers.resurrect import CrashResurrectController
from ops.controllers.wedged import WedgedAgentController
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.deploy_timing import AGENT_LEASE_SUSPEND_GAP_S, AGENT_LEASE_ZOMBIE_GRACE_S
from shared.disabled_services import is_skipped, read_skipped
from shared.log import init_gateway_process
from shared.machine import machine_name
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.restarter.daemon")

# Main-loop poll interval. env override: `AVA_RESTARTER_POLL_INTERVAL_SECONDS`.
_POLL_INTERVAL_S = settings.daemon.restarter_poll_interval_seconds
# Liveness staleness ceiling — a lightweight 1s poll never legitimately takes this
# long, so exceeding it means the loop wedged -> /healthz 503 -> watchdog respawn.
_LIVENESS_TIMEOUT_S = 60.0
_PIDFILE = settings.services.restarter_pidfile

# Post-outage grace for the lease-zombie pass (2026-08-08 audit, P1-2): when
# this daemon's own DB link was down (laptop sleep / network black-hole), every
# agent's lease expired while the DB-outage pause kept the processes alive but
# unable to renew. Without a grace window the lease-zombie reaper would kill
# all of them on its first post-outage pass, destroying the in-flight turns the
# pause exists to preserve. The values live in shared/deploy_timing.py and are
# registered in the clock lattice (shared/timing.py) — the grace must outlast
# the renew cadence, and the suspend gap is the wall-clock detector for the
# sleep case (time.time() advances through a system sleep; time.monotonic()
# does not).


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.restarter.daemon"):
        _log.info("[restarter] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.restarter.daemon") or pidfile_holds_daemon(
        legacy_pid_path("restarter"), "services.restarter.daemon"
    )


async def _dispatch_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Main loop: every second run one respawn+reap reconcile pass.

    Three controllers share this loop, all agent-process reconcilers scoped to this
    host: `RespawnController` (respawn 'restarting' rows + reap orphans),
    `HibernateController` (swap idle agents out to free RAM, swap hibernating ones
    back in on a wake), and `CrashResurrectController` (bring back agents that died
    involuntarily — 'terminated' with termination_source in 'reaper'/'launch-confirm'
    — that still have a pending inbound, so a crash with queued work is reconciled
    even when no new message arrives to trigger it). All are race-safe internally (a CAS picks the
    winner) and non-blocking. This loop owns only the schedule and the error policy:
    a `psycopg.ProgrammingError` is code<->DB drift that a retry cannot self-heal, so
    exit and let the watchdog restart after the fix; any other exception drops one
    round and is retried next tick.
    """
    respawn = RespawnController(pool)
    controllers = (
        respawn,
        HibernateController(pool),
        CrashResurrectController(pool),
        WedgedAgentController(pool),
    )
    _log.info("[restarter] daemon started, pid=%s, machine=%s", os.getpid(), machine_name())
    last_tick_wall = time.time()
    db_failures = 0
    lease_zombie_grace_until = 0.0  # wall clock; 0 = no grace armed
    while True:
        liveness.beat()
        try:
            await asyncio.sleep(_POLL_INTERVAL_S)
            now_wall = time.time()
            # The wall clock jumped ahead of the tick cadence -> this daemon (or
            # the whole host) was suspended; every agent's lease expired during
            # the sleep while the paused-but-alive processes could not renew.
            # Arm the grace window so the lease-zombie pass does not kill them
            # the moment the DB answers (audit P1-2).
            if now_wall - last_tick_wall >= AGENT_LEASE_SUSPEND_GAP_S:
                lease_zombie_grace_until = now_wall + AGENT_LEASE_ZOMBIE_GRACE_S
            last_tick_wall = now_wall
            try:
                for controller in controllers:
                    controller.reconcile("agent-runner")
            except Exception:
                db_failures += 1
                raise
            if db_failures:
                # The DB link just came back after a failure streak — arm the
                # grace window now, not at the first failure (which may be long
                # before recovery).
                lease_zombie_grace_until = time.time() + AGENT_LEASE_ZOMBIE_GRACE_S
                db_failures = 0
            respawn.set_lease_zombie_grace_until(lease_zombie_grace_until)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[restarter] schema / syntax error — code<->DB drift; retry will not "
                "self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[restarter] poll iteration failed")


async def run() -> None:
    """Start the daemon: write pidfile -> healthz server -> connect DB -> enter main loop.

    Pidfile BEFORE the healthz bind, never after: `shared.daemon_health.probe_daemon`
    rejects a 200 whose pid does not match the pidfile, so a daemon that bound the
    port while its pidfile was still unwritten would be probed as an impostor and
    killed by its own watchdog. Writing first makes "answers healthz" imply "pidfile
    already reflects me".
    """
    if _is_running():
        _log.info("[restarter] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("[restarter] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("restarter", liveness=liveness)
    _log.info("[restarter] healthz listening on :%s", health_port("restarter"))

    pool = shared.db.pool()
    try:
        await _dispatch_loop(pool, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[restarter] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop `ava cluster update` sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `shared.daemon_shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    # Belt-and-suspenders with the launchers: a stale watchdog/session launch
    # must not bypass the operator's durable disable and begin claiming agents.
    # Check before schema/DB startup so this process remains inert throughout a
    # rollout boundary even if it was spawned concurrently.
    try:
        disabled = is_skipped("restarter", read_skipped())
    except Exception:
        _log.error(
            "[restarter] disabled-services marker unreadable; refusing to start",
            exc_info=True,
        )
        return
    if disabled:
        _log.info("[restarter] durably disabled; refusing to start")
        return

    from shared.migrations import assert_schema_current
    from shared.platform import raise_fd_limit
    from shared.timing import assert_clock_lattice

    raise_fd_limit(65536)  # inherited from `ava start`'s chain; keep the ceiling raised
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    # Pre-startup sanity: the clock lattice (every load-bearing ordering between
    # timeouts/graces/poll intervals, declared in shared/timing.py) must hold for
    # the LIVE settings — an operator env override that inverts an ordering
    # (e.g. a reap grace below the launch-confirm window) fails here, loudly, on
    # the box that lives by these clocks, instead of silently re-opening the
    # 2026-07-30 spawn race.
    assert_clock_lattice()
    init_gateway_process(name="restarter")
    install_graceful_shutdown("restarter")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[restarter] interrupted, shutting down")
    except Exception:
        _log.exception("[restarter] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
