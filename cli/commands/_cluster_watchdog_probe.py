"""`ava cluster watchdog-probe --role X` — revive this capability's watchdog if dead.

The one command the OS scheduler runs (see ``shared.os_watchdog_probe`` for why
the OS, and not another Ava daemon, is what supervises the watchdog). It is
deliberately the smallest possible action: read ``role``'s watchdog ServiceSpec
out of the ops roster, decide alive-or-dead from its pidfile, and on dead hand
the spec's own ``cmd`` to the same ``respawn_service`` every healthcheck uses.

It does NOT run ``ava start``, and it does not touch any other service. A dead
watchdog is the only thing it repairs — once revived, that watchdog's next tick
reconciles its capability's services through the normal gated path (pause /
schema / pin), so a probe firing mid-``ava cluster update`` cannot resurrect something
the rollout intentionally killed. Reviving one session is also the only
action that stays correct without consulting those gates, which is what lets
this break the circular dependency (the gates live inside the watchdog process).

One exception, added after the 2026-09-17 wave-2 abort: while a maintenance
stop holds this home (a fresh marker via
``shared.os_watchdog_probe.held_stop_state``), a dead watchdog is left dead —
the stop killed it on purpose, and reviving it mid-stop aborts the stop
("services appeared during held stop"). It is the probe's only retreat from
dumb revival.
"""

from __future__ import annotations

import sys

from ops.roster import build_services
from ops.service_spec import ServiceSpec
from shared.log import logger
from shared.machine import MachineRole
from shared.proc import process_alive


def _watchdog_spec(role: MachineRole) -> ServiceSpec:
    """This capability's watchdog ServiceSpec from the ops roster.

    Read from ``ops.roster`` rather than hardcoded here so the session name, the
    launch command and the pidfile stay in ONE place — the same roster
    ``ava start`` and the watchdog's own keepalive list are derived from.
    """
    session = f"{role}-watchdog"
    for spec in build_services():
        if spec.session == session:
            return spec
    raise ValueError(f"no ServiceSpec named {session!r} in the ops roster")


def _alive(spec: ServiceSpec) -> bool:
    """True when the watchdog's pidfile names a live process.

    The watchdog has no HTTP healthz (it is not a server), so the pidfile IS the
    liveness signal — the same one ``ava status`` reports as "(pid)" and the
    daemon itself uses to refuse a duplicate start. ``process_alive`` rather than
    a raw ``os.kill(pid, 0)``: on Windows that call terminates the target.
    """
    pidfile = spec.pidfile
    if pidfile is None:
        raise ValueError(f"{spec.session} has no pidfile; cannot probe liveness")
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    return process_alive(pid)


def cmd_watchdog_probe(role: MachineRole) -> int:
    """Probe ``role``'s watchdog and respawn it when dead.

    Returns 0 when the watchdog is alive, when a fresh held-stop marker says a
    maintenance stop holds this home (the respawn is deliberately skipped — see
    the module docstring), or when the respawn succeeded; 1 when the respawn
    failed. The scheduler discards the exit code either way; it is
    the log line that an operator reads, and a non-zero code that shows up in
    ``launchctl list``.
    """
    from shared.os_watchdog_probe import HELD_STOP_TTL_S, HeldStopState, held_stop_state
    from shared.paths import repo_root
    from shared.service_respawn import respawn_service

    spec = _watchdog_spec(role)
    state = held_stop_state()
    if state is HeldStopState.FRESH:
        # Deliberately before the pidfile read: during a held stop the pidfile
        # says nothing the probe may act on. Logged so the silence is visible.
        logger.info("[watchdog-probe] {}: maintenance stop in progress; standing down", role)
        return 0
    if _alive(spec):
        return 0

    if state is HeldStopState.STALE:
        logger.warning(
            "[watchdog-probe] {}: held-stop marker is older than {}s; reviving",
            role,
            int(HELD_STOP_TTL_S),
        )
    elif state is HeldStopState.UNREADABLE:
        logger.warning("[watchdog-probe] {}: held-stop marker is unreadable; reviving", role)
    logger.warning("[watchdog-probe] {} watchdog is down; respawning", role)
    # force=True: the probe is the recursion's dumb-revival leg (see the module
    # docstring) — it must not be held back by the source-switch window, or a
    # dead watchdog stays dead through the whole update instead of being
    # revived (a mid-checkout revive may crash on a torn import and be retried
    # next minute, which is the probe's designed behavior).
    if not respawn_service(spec.session, spec.cmd, repo_root(), force=True):
        logger.error("[watchdog-probe] failed to respawn {} watchdog", role)
        return 1
    logger.info("[watchdog-probe] {} watchdog respawned", role)
    return 0


def cmd_watchdog_probe_register(role: MachineRole) -> int:
    """Register the OS-scheduled watchdog probe for ``role`` (manual / debug entry)."""
    from shared.os_watchdog_probe import register_watchdog_probe

    try:
        register_watchdog_probe(role)
    except RuntimeError as e:
        print(f"  * {e}", file=sys.stderr)
        return 1
    return 0


def cmd_watchdog_probe_unregister(role: MachineRole) -> int:
    """Remove the OS-scheduled watchdog probe for ``role`` (manual / debug entry)."""
    from shared.os_watchdog_probe import unregister_watchdog_probe

    unregister_watchdog_probe(role)
    return 0
