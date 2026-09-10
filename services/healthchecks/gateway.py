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

**Pause-scoped exemption + respawn gate** (issue #2101): a pause-scoped watchdog
block still runs this check (the gateway capability watchdog exempts it — see
`services/watchdog/daemon.py:_checks_for_round`), because a gateway that is
completely down is worse than a probe under a paused host. The respawn itself is
gated: while the pause still has a live owner (an executing lease, a live local
orchestration, a maintenance hold) the respawn declines — a rollout's own
restart leg is never raced. Once the owner determination (with the gateway
reachability evidence) reads unowned, the respawn proceeds even while the pause
controller has not recovered yet.

**Off-pin converge-back** (issue #2101): when the checkout drifted off the
cluster pin (a dead rollout checked out the target but never migrated), the
gateway cannot boot from its own tree — `assert_schema_current` refuses code
ahead of the DB. `_restart` then converges the checkout back to the pinned
commit before respawning, so the gateway returns on the code the whole cluster
runs. The cluster pin is never written, no update mechanism runs, and the
converge is skipped while any orchestration session, live deploy lease, or
source switch is in flight.

Usage (watchdog daemon):
    every 60s `services.watchdog.daemon` spawns a thread to call this main().
"""

import logging
import os
import signal
import subprocess
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
    _converge_off_pin_checkout(project_root)
    return respawn_and_verify(
        "gateway",
        ".venv/bin/python -m gateway",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
        verify=_probe,
    )


def _converge_off_pin_checkout(repo: Path) -> None:
    """Converge a drifted checkout back to the cluster pin before the respawn.

    A dead rollout can leave the prod source on the target commit with the pin
    un-advanced and the migrations unapplied; the gateway cannot boot from that
    tree (`assert_schema_current` refuses code ahead of the DB), and respawning
    it there only burns backoff rounds. The pinned binary is the one that
    matches the applied schema, so the checkout returns to the pin — the same
    converge direction the agent-runner pin heal takes, minus the update
    machinery: no lease, no fan-out, no migration, no pin write, nothing else
    restarted. Guards skip the converge while any orchestration session, live
    deploy lease, or source switch is in flight (a live rollout owns its own
    checkout), and every read is conservative: unreadable evidence skips.
    """
    from ops.cluster_session import live_orchestration_session
    from ops.controllers.pin import read_pin_and_head
    from shared import source_switch
    from shared.cluster_lock import read_update_lease
    from shared.gitenv import git_env
    from shared.proc import run_bounded

    try:
        if live_orchestration_session() is not None or read_update_lease() is not None:
            return
    except Exception:
        _log.warning(
            "[gateway healthcheck] could not confirm no orchestration is in "
            "flight; skipping the off-pin converge"
        )
        return
    if source_switch.is_switching():
        return
    pin_head = read_pin_and_head()
    if pin_head is None:
        return
    pin, head = pin_head
    if head == pin:
        return
    _log.warning(
        "[gateway healthcheck] off-pin (HEAD %s != pin %s) and the gateway is down — "
        "converging the checkout back to the pin before respawning (the cluster pin "
        "is not moved)",
        head,
        pin,
    )
    source_switch.mark_switching()
    try:
        try:
            result = run_bounded(
                ["git", "-C", str(repo), "checkout", "--detach", "--force", pin],
                capture_output=True,
                text=True,
                env=git_env(),
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError):
            _log.warning(
                "[gateway healthcheck] off-pin converge checkout failed; "
                "respawn proceeds on the existing tree",
                exc_info=True,
            )
            return
    finally:
        source_switch.clear_switching()
    if result.returncode != 0:
        _log.warning(
            "[gateway healthcheck] off-pin converge checkout failed (rc=%s); "
            "respawn proceeds on the existing tree",
            result.returncode,
        )


def _pause_respawn_gate() -> tuple[bool, str]:
    """Decline the respawn while a paused host's transition still has an owner.

    The pause-scoped exemption runs this check under a paused host; probing is
    always allowed, but respawning must not fight a live rollout's own restart
    leg. The owner determination is the stranded-pause controller's own
    (including the gateway reachability evidence), so the two recoveries agree.
    """
    from ops.controllers.stranded_pause import is_paused, pause_owner_verdict

    if not is_paused():
        return True, ""
    owner = pause_owner_verdict()
    if owner is None:
        return True, ""
    return False, f"the pause still has an owner ({owner})"


def main() -> None:
    init_gateway_process(name="gateway-healthcheck")
    # Two failed rounds deliberately self-heal a sustained DB outage by respawning
    # about every two minutes, accepting stats-cache loss. PgBouncer pool poisoning
    # has a precedent; a respawn is the clean reconnect path. Change this only with
    # a fresh decision, not as an accidental retry-policy simplification.
    run_keepalive(
        "gateway",
        _log,
        probe=_probe,
        respawn=_restart,
        consecutive_failures_before_respawn=2,
        respawn_gate=_pause_respawn_gate,
    )


if __name__ == "__main__":
    main()
