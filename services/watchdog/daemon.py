"""Watchdog daemon — runs ONE capability's service healthchecks every 60s.

Replaces OS cron (#247 follow-up) — macOS Full Disk Access restrictions
make cron write operations hang in certain terminal contexts (Claude
Code shell without FDA: `crontab -` / `crontab <file>` both hang).
Watchdog is fully user-space (runs in a supervised session, same tier as
other services); consistent behavior across macOS/Linux, no dependency
on the system scheduler.

One watchdog runs PER CAPABILITY, selected by `--role`: `gateway` watches the
gateway daemons (gateway/labeler/heartbeat/task-maintenance/milvus/memory-indexer/
frontend), `agent-runner` watches ops/agent-host (+ browser/browser-mcp).
The roster is derived from `build_services()` — see `_checks_for_capability`. A
single-box gateway,agent-runner host
runs BOTH (two supervised sessions, two pidfiles). The previous single role-union
daemon collided when two co-located units shared one host: the lone surviving
`ava-watchdog` covered only one unit's capability, leaving the other's
services unrevived.

Reconcile gates now live in the ops controller-manager (`ops.manager` +
`ops.controllers.*`): each round `_tick` runs the controller list (pause →
schema → pin) and then runs the healthchecks a blocking controller left in scope.
The gate logic was extracted from this daemon so it has one home and this daemon
stays a thin main over the manager.

A blocking controller reports a `BlockScope`, not a verdict on the roster: `ALL`
(this host is mid-transition — paused, or an update just spawned) or `DB_DEPENDENT`
(the DB is unreachable / disagrees with this code). This daemon is the one place
that resolves a scope into a roster, by matching it against each service's own
`ServiceSpec.requires_db` (`_checks_for_round`). That split is deliberate: a
DB-scoped block used to skip the WHOLE round, so a Postgres outage also stopped
`browser` / `browser-mcp` — two services with no DB at boot or at runtime — from
being revived, and the recovery path for an unrelated Chrome crash went down with
the database it never used.

The watchdog IS monitored, by the OS scheduler: `shared.os_watchdog_probe`
registers one launchd/crontab job per capability that runs `ava cluster
watchdog-probe --role <role>` every 60s and respawns this session when its
pidfile shows it dead. That terminates the recursion outside Ava, in a
supervisor that is itself externally supervised (launchd is pid-1-adjacent;
crond is revived by init).

This used to be an accepted gap ("if the session dies the user manually runs
`ava start`; for a single-user system the recursion is acceptable"). A
multi-machine fleet broke that assumption: a runner's watchdog died and its
services stayed down for hours with nobody watching, and boot autostart could
not help because `@reboot` / `RunAtLoad` fire once at boot and the box had been
up for days.

Usage:
    .venv/bin/python -m services.watchdog.daemon --role gateway
    .venv/bin/python -m services.watchdog.daemon --role agent-runner

Same pattern as other services: `ava start` spawns sessions
`ava-gateway-watchdog` / `ava-agent-runner-watchdog`;
`ava stop` graceful (PR #247 send-keys C-c) exits.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ops.controllers.base import BlockScope
from ops.manager import ControllerManager
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

# The statically imported healthchecks are the ones with NO ServiceSpec in
# build_services(): brew-pin asserts host package policy, redis/pgbouncer are
# native per-cluster processes, permissions-helper is a launchd-owned app, and
# the LGTM stack is native launchd jobs (deploy/lgtm), so they are not part of the
# build_services-derived roster.
# Every other healthcheck is resolved from its ServiceSpec.healthcheck_module via
# importlib (see _checks_for_capability), so build_services() stays the single
# source of the keepalive roster.
from services.healthchecks.brew_pin import main as brew_pin_healthcheck
from services.healthchecks.lgtm import main as lgtm_healthcheck
from services.healthchecks.permissions_helper import main as permissions_helper_healthcheck
from services.healthchecks.pgbouncer import main as pgbouncer_healthcheck
from services.healthchecks.redis_acl import main as redis_acl_healthcheck
from shared import telemetry
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.disabled_services import is_skipped, read_skipped
from shared.log import init_gateway_process
from shared.machine import MachineRole

_log = logging.getLogger("services.watchdog.daemon")

# 60s matches the original cron `* * * * *` behavior. Add env override
# for dev/test (e2e tests do not want to wait 60s per round).
_INTERVAL_S = settings.services.watchdog_interval_seconds

# A controller or healthcheck that never returns cannot leave this process
# looking healthy forever. The deadline exceeds the 60s cadence by enough to
# allow a normal sequential round, while producing an explicit skipped round
# instead of a permanently wedged watchdog.
_TICK_DEADLINE_S = 90.0

# The reconcile controllers this daemon runs before its healthchecks. A single
# module-level manager so its per-dimension last-result state (surfaced by
# ops.observe) persists across ticks.
_manager = ControllerManager()


@dataclass(frozen=True)
class _Check:
    """One entry of a round's healthcheck roster.

    ``requires_db`` is carried through from the service's own declaration
    (``ServiceSpec.requires_db``, or the literal beside a pseudo-check below) so
    ``_checks_for_round`` can honor a DB-scoped block without knowing anything about
    the individual services.
    """

    name: str
    run: Callable[[], None]
    requires_db: bool


@dataclass
class _TickProgress:
    """The wall-clock freshness fact shared by health and Prometheus."""

    last_completed_at: float | None = None
    in_flight: asyncio.Task[None] | None = None

    def record_completed(self) -> None:
        """Record one fully completed round after all controller/check work returned."""
        self.last_completed_at = time.time()
        telemetry.emit(
            "telemetry",
            "watchdog_tick",
            attributes={"last_tick_timestamp_seconds": self.last_completed_at},
        )


def _consume_tick_exception(task: asyncio.Task[None]) -> None:
    """Retrieve an exception from a deadline-detached tick.

    A deadline stops awaiting the task but deliberately does not cancel it: a
    synchronous worker thread continues until its I/O returns. Reading a late
    exception prevents asyncio from reporting an unobserved task failure while
    the next cadence waits for that tick to leave the in-flight guard.
    """
    if not task.cancelled():
        task.exception()


# Each capability's watchdog uses its own pidfile so the two daemons on a
# single-box host do not mistake each other's pid for a duplicate start.
def _pidfile_for_role(role: MachineRole) -> Path:
    if role == "gateway":
        return settings.services.gateway_watchdog_pidfile
    if role == "agent-runner":
        return settings.services.agent_runner_watchdog_pidfile
    raise ValueError(f"unknown watchdog role: {role!r}")


def _health_name_for_role(role: MachineRole) -> str:
    """The distinct health listener for one capability watchdog."""
    if role not in ("gateway", "agent-runner"):
        raise ValueError(f"unknown watchdog role: {role!r}")
    return f"{role.replace('-', '_')}_watchdog"


def _resolve_healthcheck(module: str) -> Callable[[], None]:
    """Import a healthcheck module's ``main`` — the ``() -> None`` the watchdog runs
    each tick. Every ``services.healthchecks.<x>`` module exposes ``main`` by
    convention; a bad module string is a build_services() bug and is caught in CI by
    ``tests/services/test_watchdog_roster.py`` (so this never fails at runtime).
    ``importlib.import_module`` returns the already-loaded module from ``sys.modules``
    after the first tick, so re-resolution each round is cheap."""
    return importlib.import_module(module).main


def _runner_watchdog_owns_lgtm() -> bool:
    """Whether the agent-runner watchdog must keep the native LGTM stack alive.

    The same single provider-identity decision the lgtm healthcheck itself
    gates on (`home_is_observability_station`: lgtm-host marker OR
    observability-station capability, shared/observability.py) — a station on
    a non-gateway machine is brought up by converge and must be repaired by
    this watchdog, not left to die silently. Marker absent + no capability =
    False, so a pure runner's roster is byte-for-byte unchanged.
    """
    from shared.observability import home_is_observability_station
    from shared.paths import ava_home

    return home_is_observability_station(ava_home())


def _checks_for_capability(role: MachineRole) -> list[_Check]:
    """The healthcheck roster for the watchdog's own capability (the `--role` arg),
    NOT a union derived from machine_role(): the two co-located watchdogs each own
    exactly one capability's services.

    SINGLE SOURCE OF TRUTH — the roster is derived from `ops.spec` via
    `services_for_capabilities_annotated(frozenset({role}))`, the same
    role-membership + config/capability gating that `ava start` launches. Any
    service that declares a `ServiceSpec.healthcheck_module` is auto-wired here; a
    new service is kept alive the moment it is registered in the ops roster — no
    watchdog edit. A gated-out service (browser incapable,
    heartbeat/task-maintenance disabled) is skipped with a debug line (fires every
    60s; the durable operator surface is `ava status`), so a revive can never
    crash-loop a service `ava start` chose not to launch.

    Six pseudo-checks have NO ServiceSpec (they are not session-backed services) and are
    added by hand — so they state their own
    ``requires_db`` right here, the same fact the other entries carry from their spec:
    - redis-acl FIRST — repairs the per-cluster redis ACL user; every daemon below
      depends on redis auth, and a redis-server restart drops the in-memory ACL, so
      it must run before reviving anything that would crash on AuthenticationError.
      Redis-only (`requires_db=False`): the ACL repair is exactly the kind of work a
      Postgres outage has no claim on.
    - pgbouncer SECOND, ahead of every service — when the pooler is enabled it IS
      every consumer's `AVA_DB_URL`, so reviving a daemon before it is back only
      produces a daemon that cannot reach the database. `requires_db=False` for the
      same reason redis-acl is: a DB-scoped block exists because the database is
      unreachable, and holding back the check that repairs the front door to it
      would be a deadlock — the pooler probe is the admin console, which needs no
      backend.
    - brew-pin on BOTH capabilities — detects drift from the operator-approved
      Homebrew pin set on any macOS unit. It is warning-only and host-local, so
      neither role ownership nor database availability should suppress it
      (``requires_db=False``).
    - permissions-helper on the AGENT-RUNNER capability when enabled — probes the
      launchd-owned helper's real protocol and repairs one persistent failure
      episode. It needs no Postgres (``requires_db=False``).
    - station-probe on the GATEWAY capability — the remote observatory
      station's health (WP4, task #1946). Probe-only: never restarts anything,
      alerts fail-open. ``requires_db=True`` because it resolves the station's
      advertised address from machine_units; holding it back during a
      DB-scoped block is exactly fail-open. The module lives in
      services/heartbeat/ (not services/healthchecks/) because it consumes the
      gateway-owned ``alerts`` domain — see the check's comment below.
    pg-backup is instead a regular DB-dependent `ServiceSpec` scheduler. Its
    healthcheck probes last-success age and never runs a dump in this round.
    """
    # The roster lives in ops.spec (services < ops is fine; this also drops the
    # old services->cli edge the roster import used to carry). Local import keeps
    # the module-load graph light and matches the pre-existing deferred pattern.
    from ops.spec import services_for_capabilities_annotated

    if role not in ("gateway", "agent-runner"):
        raise ValueError(f"unknown watchdog role: {role!r}")

    checks: list[_Check] = []
    if role == "gateway":
        if settings.data_plane.is_remote:
            # Remote-managed data plane: there is no local Redis ACL user or
            # PgBouncer to repair — both repairs target the per-cluster local
            # instance and would mis-aim at a foreign service.
            _log.debug(
                "[watchdog] remote-managed data plane — skipping the local "
                "redis-acl and pgbouncer checks"
            )
        else:
            checks.append(_Check("redis-acl", redis_acl_healthcheck, requires_db=False))
            checks.append(_Check("pgbouncer", pgbouncer_healthcheck, requires_db=False))
    checks.append(_Check("brew-pin", brew_pin_healthcheck, requires_db=False))
    if role == "agent-runner" and settings.services.permissions_helper_enabled:
        checks.append(
            _Check(
                "permissions-helper",
                permissions_helper_healthcheck,
                requires_db=False,
            )
        )
    for spec, gate_reason in services_for_capabilities_annotated(frozenset({role})):
        if spec.healthcheck_module is None:
            continue  # not watchdog-monitored (the watchdog daemons themselves)
        if gate_reason is not None:
            # config/capability-gated OUT of `ava start`'s roster, so do not revive
            # it either (a fail-fast daemon would just crash-loop every round).
            _log.debug("[watchdog] %s not revived: %s", spec.session, gate_reason)
            continue
        checks.append(
            _Check(
                spec.session,
                _resolve_healthcheck(spec.healthcheck_module),
                requires_db=spec.requires_db,
            )
        )
    if role == "gateway":
        # lgtm: the observability-backend native stack (deploy/lgtm) — no
        # ServiceSpec (launchd jobs, not a session). The check gates itself
        # on the $AVA_HOME/lgtm-host marker OR the observability-station
        # capability, so on every non-station host it is a no-op. The stack
        # needs no Postgres: `requires_db=False` — the observability read path
        # must not be held hostage by a DB outage.
        checks.append(_Check("lgtm", lgtm_healthcheck, requires_db=False))
        # station-probe: the REMOTE observatory station's health (WP4, task
        # #1946) — no ServiceSpec, it is a probe-only check. Self-gating on
        # AVA_OBSERVABILITY_URL (empty = local observatory, the lgtm check
        # owns it; non-empty = the gateway dials the station's advertised
        # OTLP ingress with the cluster bearer and alerts fail-open on
        # failure). requires_db=True — it resolves the advertised address
        # from machine_units; a DB block holds it back, which is exactly
        # fail-open.
        #
        # Resolved by dotted string, NOT imported at module level: the module
        # consumes the gateway-owned `alerts` settings domain, and a static
        # import here would drag that domain into the runner profile
        # (test_gateway_consumer_guard). The runner watchdog never runs this
        # check — only the gateway branch appends it.
        checks.append(
            _Check(
                "station-probe",
                _resolve_healthcheck("services.heartbeat.station_probe"),
                requires_db=True,
            )
        )
    elif _runner_watchdog_owns_lgtm():
        # A station-capable agent-runner (marker OR observability-station
        # capability, no gateway) also owns the host's native backends: its
        # converge brings the stack up, so this capability's watchdog must keep
        # it alive — same self-gating check, same require_db=False. A pure
        # runner (no station identity) stays untouched: no check, zero
        # regression (task #1945, WP3).
        checks.append(_Check("lgtm", lgtm_healthcheck, requires_db=False))

    # Honor an operator's durable `ava start --disable-service X`: `_gate_reason`
    # does not cover the disable-service marker, so filter it here (also covers the
    # pseudo-checks). Names normalize across kebab/snake (disabled_services.is_skipped).
    skipped = read_skipped()
    if skipped:
        kept = [c for c in checks if not is_skipped(c.name, skipped)]
        dropped = sorted({c.name for c in checks} - {c.name for c in kept})
        if dropped:
            # debug, not info: this fires every 60s round; the durable record is
            # the marker file + the `ava start` "(skip via --disable-service)" line.
            _log.debug(
                "[watchdog] honoring --disable-service, not reviving: %s", ", ".join(dropped)
            )
        return kept
    return checks


def _checks_for_round(role: MachineRole, blocks: BlockScope) -> list[_Check]:
    """The checks this round may actually run, given what the controllers blocked.

    THE one place a ``BlockScope`` is resolved against the roster — the controllers
    say how wide their finding is, the services say what they need, and the match
    happens here. Total over the enum: an unhandled member raises rather than
    silently falling through to "run everything" (or to "run nothing"), either of
    which would be a safety decision made by omission.

    ``ALL`` returns before the roster is even built, so a paused / mid-update host
    keeps doing exactly no work per round, capability probes included.
    """
    if blocks is BlockScope.ALL:
        return []
    roster = _checks_for_capability(role)
    if blocks is BlockScope.NONE:
        return roster
    if blocks is BlockScope.DB_DEPENDENT:
        kept = [c for c in roster if not c.requires_db]
        held = [c.name for c in roster if c.requires_db]
        if held:
            # info, not debug: unlike the gated-out services this is a transient,
            # incident-shaped state, and "which services were held back while the DB
            # was down" is what an operator reads this log for afterwards.
            _log.info(
                "[watchdog] DB-scoped block: holding back %s; still checking %s",
                ", ".join(held),
                ", ".join(c.name for c in kept) or "(nothing)",
            )
        return kept
    raise ValueError(f"unhandled block scope: {blocks!r}")


def _write_pidfile(pidfile: Path) -> None:
    if not acquire_pidfile(pidfile, "services.watchdog.daemon"):
        _log.info("[watchdog] daemon already running (pidfile=%s), exiting", pidfile)
        sys.exit(1)


def _remove_pidfile(pidfile: Path) -> None:
    remove_pidfile(pidfile)


def _is_running(pidfile: Path) -> bool:
    """Check whether this capability's watchdog is already running (via pidfile).

    Checks the new pidfile path first, then falls back to the legacy
    $AVA_HOME/<name>.pid location for backward compat during the
    transition to the run/ subdirectory. Pid-reuse-safe: a live pid whose
    argv does not name the watchdog module is a recycled pid, not a running
    watchdog (audit round 2, P1)."""
    # Derive the role from which pidfile was passed to pick the right legacy name.
    legacy_names: list[str] = []
    if pidfile == settings.services.gateway_watchdog_pidfile:
        legacy_names.append("gateway-watchdog")
    elif pidfile == settings.services.agent_runner_watchdog_pidfile:
        legacy_names.append("agent-runner-watchdog")
    from shared.paths import legacy_pid_path

    return any(
        pidfile_holds_daemon(p, "services.watchdog.daemon")
        for p in [pidfile] + [legacy_pid_path(n) for n in legacy_names]
    )


# Per-check last failure code the watchdog logged, so a healthcheck that keeps
# exiting with the SAME code does not earn a fresh ERROR line every round — one
# line per failure episode, reset the first round the check stops failing. A
# persistent terminal verdict (exit 3, e.g. the browser's CDP port held by
# another unit) produced two ERROR lines per round forever (one from the
# healthcheck, one here); 1.8k lines/day across machine-1 + win is pure noise, and
# the healthcheck's own episode-gated reporting already carries the condition.
_last_failure_code: dict[str, str | int | None] = {}


async def _run_check(name: str, fn: Callable[[], None]) -> None:
    """Run a single healthcheck function in a thread.

    Healthchecks use sync subprocess.Popen to start service processes;
    thread isolation avoids blocking the main asyncio loop. Exceptions
    are caught and logged so one failing healthcheck does not crash the
    whole watchdog (matches cron behavior: each cron fork is
    independent; failure only drops one round).

    `SystemExit` is caught alongside `Exception` and is NOT redundant: the
    terminal verdict exits `EXIT_PORT_TAKEN` (3) and the browser healthcheck
    (which does not use `run_keepalive`) exits `EXIT_RESPAWN_FAILED` (1) — the
    right contract when cron runs a module standalone, but `SystemExit` is a
    `BaseException`, so an `except Exception` alone would let it escape
    `to_thread` and unwind the whole daemon. The watchdog that exists to revive
    dead services would itself die on the first service it could not revive,
    exactly when it is needed most. (The keepalive healthchecks no longer exit
    on a failed respawn — task #1941: the round reports the scheduled backoff
    and returns; their failure signal is the WARNING lines and the
    `respawn_breaker_open` event, see [[healthchecks/terminal-verdict/
    terminal-verdict.ava.okf.md]].)

    The code is logged because it carries meaning: `EXIT_RESPAWN_FAILED` (1) is
    "respawned and it did not come up" — since #1941 raised only by the browser
    healthcheck, while `EXIT_PORT_TAKEN` (3) is a daemon this unit cannot revive
    at all — another cluster holds its port — which no further round will fix
    ([[healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]).

    The line itself is emitted per episode, not per round: a repeated exit with
    the same code logs once, and quiet rounds log at DEBUG so the condition
    stays visible in the log. A round where the check does not fail (or raises)
    resets the episode, so the next failure is a new first sight.
    """
    try:
        await asyncio.to_thread(fn)
    except SystemExit as exc:
        code = exc.code
        if _last_failure_code.get(name) == code:
            _log.debug(
                "[watchdog] healthcheck %s reported failure (exit %s) — same code as last "
                "round, not re-logged",
                name,
                code,
            )
            return
        _last_failure_code[name] = code
        _log.error("[watchdog] healthcheck %s reported failure (exit %s)", name, code)
        return
    except Exception:
        _log.exception("[watchdog] healthcheck %s raised", name)
        return
    _last_failure_code.pop(name, None)


async def _tick(role: MachineRole) -> None:
    """One round for ONE capability: run the reconcile controllers, then run the
    healthchecks they left in scope, sequentially.

    The controller-manager (`ops.manager`) runs the controller list in order:
    0. **updater** — reap an `ava-updater` session that has stopped writing. First
       because every controller below defers to that session, so a corpse holding the
       name stalls all of them; it never blocks, so the rest of the round runs on a
       corrected reading of the host.
    0b. **rollout** — an `ava-rollout` orchestration that has stopped writing is
       interrupted (`SIGINT` to the pid its deploy-lease holder names) so it runs its
       own abort; force-killed only if that changes nothing for another full window.
       Gateway only, and ahead of `pause` because a rollout pauses this host before it
       reaches anything that can hang — this round IS the one blocked by that pause.
    1. **pause** — the paused posture the gateway writes during `ava cluster update`
       Phase A. A paused host skips the round entirely (`ALL`; otherwise this tick's
       host healthcheck would revive the host Phase A drained and stopped); stranded-pause recovery self-unpauses a pause that outlived its rollout.
    2. **schema** — DB applied vs local code required mismatch, or an unreachable DB.
       Blocks the services that USE the DB (`DB_DEPENDENT`) so a spawned old-code
       daemon does not immediately crash on the new schema; self-heals code-behind via
       `ava cluster update` (which, once spawned, IS a whole-host transition → `ALL`), waits
       on DB-behind.
    3. **pin** — HEAD != cluster pin. Capability-split: the agent-runner watchdog
       acts (force-update to the pin, then skips the round — `ALL`); the gateway
       watchdog only warns. Declines while any update is in flight here, lease or not:
       off-pin is a running update's own mid-flight state.
    4. **code** — HEAD IS the pin but the running processes are not. The narrowest
       reading of "wrong code here", so it runs last: it only applies once `pin` ahead
       of it has made the checkout right. Heals with a restart, not a checkout
       (agent-runner only).

    Each controller reconcile is offloaded to a thread inside the manager, so a
    blocking DB/HTTP call never freezes the event loop. Whatever the first blocking
    controller reports is resolved against the roster by `_checks_for_round`.
    """
    blocks = await _manager.reconcile(role)
    for check in _checks_for_round(role, blocks):
        await _run_check(check.name, check.run)


async def _run_tick_with_deadline(role: MachineRole, progress: _TickProgress) -> bool:
    """Run one watchdog round within its hard deadline.

    ``asyncio.timeout`` stops awaiting a stalled round, but its synchronous
    controller/check worker cannot be killed safely. The in-flight task stays
    shielded from that cancellation; later cadences skip the whole round until
    it finishes, so no controller or healthcheck has concurrent copies racing
    each other. A deadline deliberately leaves freshness stale rather than
    recording a false completion timestamp.
    """
    previous = progress.in_flight
    if previous is not None:
        if not previous.done():
            _log.error(
                "[watchdog] %s prior tick is still running; skipping this round",
                role,
            )
            return False
        progress.in_flight = None

    tick = asyncio.create_task(_tick(role), name=f"watchdog-{role}-tick")
    tick.add_done_callback(_consume_tick_exception)
    progress.in_flight = tick
    try:
        async with asyncio.timeout(_TICK_DEADLINE_S):
            await asyncio.shield(tick)
    except TimeoutError:
        _log.error(
            "[watchdog] %s tick exceeded %.1fs; skipping the rest of the round",
            role,
            _TICK_DEADLINE_S,
        )
        return False
    finally:
        if tick.done():
            progress.in_flight = None
    progress.record_completed()
    return True


async def run(role: MachineRole) -> None:
    """Start the daemon for one capability: write its pidfile -> enter main loop.

    The first _tick() is **intentionally deferred by one _INTERVAL_S**.
    `ava start`/cmd_start spawns sessions sequentially per
    build_services(); watchdog is last. After spawn completes, the watchdog
    daemon's asyncio loop comes up immediately, at which point the
    earlier services are still in import / bind-port phases (interpreter
    cold start + uvicorn lifespan typically 2-5s). If the loop ran
    _tick() right away, the gateway HTTP probe would connection-refused
    -> the healthcheck would respawn_service and kill the just-started
    gateway and restart it, racing cmd_start. This recurred in history:
    after ava cluster update, prod gateway was running stale code; manual
    `ava stop && ava start` fixed it (see the
    PR-D deploy).

    Sleep one interval first to let services warm up; if something
    really died it gets caught in the next round, and a 60s delay is
    acceptable (watchdog is not sub-second monitoring). This is
    equivalent to "daemon startup is an implicit ack that services are
    ready", naturally aligned with the cmd_start fire-and-forget
    contract.
    """
    pidfile = _pidfile_for_role(role)
    if _is_running(pidfile):
        _log.info("[watchdog] %s daemon already running (pidfile=%s), exiting", role, pidfile)
        sys.exit(1)

    _write_pidfile(pidfile)
    _log.info("[watchdog] %s pidfile written: %s", role, pidfile)
    health_name = _health_name_for_role(role)
    progress = _TickProgress()
    liveness = Liveness(_TICK_DEADLINE_S)
    health: asyncio.Server | None = None
    _log.info(
        "[watchdog] %s daemon started, interval=%.1fs (first tick delayed)", role, _INTERVAL_S
    )
    try:
        health = await start_health_server(
            health_name,
            liveness=liveness,
            extra=lambda: {"last_tick_at": progress.last_completed_at},
        )
        _log.info("[watchdog] %s healthz listening on :%s", role, health_port(health_name))
        while True:
            # sleep first, tick second — see docstring re cmd_start race.
            await asyncio.sleep(_INTERVAL_S)
            if await _run_tick_with_deadline(role, progress):
                liveness.beat()
    finally:
        if health is not None:
            await stop_health_server(health)
        _remove_pidfile(pidfile)
        _log.info("[watchdog] %s daemon stopped", role)


def main() -> None:
    """Entry point: parse --role, init logger + run asyncio loop.

    SIGTERM (the graceful stop `ava cluster update` sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `shared.daemon_shutdown`. `ava stop`
    default force-kill does not reach this.
    """
    from shared.platform import raise_fd_limit

    raise_fd_limit(65536)  # this daemon spawns every healthcheck; keep the ceiling raised
    parser = argparse.ArgumentParser(description="Per-capability watchdog daemon.")
    parser.add_argument(
        "--role",
        required=True,
        choices=["gateway", "agent-runner"],
        help="Which capability's services this watchdog watches.",
    )
    role: MachineRole = parser.parse_args().role
    # Each capability watchdog writes its own log so a single-box host's two
    # watchdogs do not interleave (gateway-watchdog.log / agent-runner-watchdog.log).
    init_gateway_process(name=f"{role}-watchdog")
    install_graceful_shutdown(f"{role}-watchdog")
    try:
        asyncio.run(run(role))
    except KeyboardInterrupt:
        _log.info("[watchdog] interrupted, shutting down")
    except Exception:
        _log.exception("[watchdog] daemon crashed — uncaught exception escaped run()")
        raise


if __name__ == "__main__":
    main()
