"""Hand a wrong-domain macOS `ava start` over to the GUI domain (task #3348).

Service sessions inherit the launchd management domain of the chain that spawns
them, and no in-place relaunch can move one — so a start whose own chain runs
outside the GUI login session seeds every session it launches with the wrong
domain (the state that wedges the headed browser; tasks #3149/#3346). This
module implements the R2b handover: when such a chain is detected on an
operator-shaped start, the bring-up is handed to the cluster's GUI-domain
autostart job (`shared.os_autostart.ensure_via_gui_domain` — `kickstart -p`,
which never kills a running instance), and this process waits for readiness
and answers with the same exit contract as a normal start.

Internal shapes never hand over. A rollout child carries the credential
handover marker in its process env, and that marker cannot cross into a
launchd job (the job's env comes from the plist; env-file/argv transport is
forbidden by design); `--persist-services` / `--disable-service` /
`--updater-telemetry` invocations are not equivalent to the canonical job's
bare `ava start`, so those keys are dropped rather than re-homed. Such starts
keep the old behavior (plus the R2a warning), and unit-chain hygiene rides on
the chain's origin staying in the GUI domain.
"""

from __future__ import annotations

import sys
import time
from functools import partial

from cli.commands._pause_resume import StartDelegation
from cli.commands._start_gui_chain import _rehomeable_domain
from shared.cluster import session_name
from shared.deploy_timing import SERVICE_READY_TIMEOUT_S
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from shared.machine import MachineRoles
from shared.paths import running_sha_path

_LAUNCH_SETTLE_S = 2.0
"""After the job's running-sha bookmark advances, give the spawn burst a beat
before the readiness wait starts judging absence: the wait's gone-session early
exit (two consecutive polls, `_SESSION_GONE_CONFIRMATIONS` x `_READY_POLL_
INTERVAL_S`) would otherwise read a session the job has not spawned yet as a
dead one."""

_LAUNCH_SIGNAL_TIMEOUT_S = 90.0
"""How long to wait for the job to reach its launch step before judging the
probes directly. A start that never gets there (a failed converge, a job that
died) leaves its reason in the job's own log (`$AVA_HOME/logs/autostart.log`)."""

_LAUNCH_SIGNAL_POLL_S = 0.5


def _job_reached_launch_step(t0: float) -> bool:
    """True once the job's `ava start` has advanced the running-sha bookmark.

    `record_running_sha` runs one statement before `_launch_sessions`, so a
    bookmark mtime newer than `t0` (taken before the kick) means the job's
    launch pass is (about to be) underway. A missing or unreadable file is not
    evidence either way."""
    try:
        return running_sha_path().stat().st_mtime > t0
    except OSError:
        return False


def _await_launch_step(t0: float, timeout_s: float | None = None) -> bool:
    """Poll until the job reaches its launch step; False once `timeout_s` passes."""
    bound = _LAUNCH_SIGNAL_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + bound
    while True:
        if _job_reached_launch_step(t0):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LAUNCH_SIGNAL_POLL_S)


def _maybe_handover_start(
    roles: MachineRoles,
    *,
    disabled_services: tuple[str, ...],
    persist_services: bool,
    updater_telemetry: bool,
    parent_handoff: bool,
    readiness_gate: bool,
) -> int | StartDelegation | None:
    """Hand the bring-up to the GUI domain when this start must not run in place.

    Return a deferred launch for the lifecycle wrapper to run after releasing
    its lock, an early refusal, or None for an in-place start. A failed GUI
    launch fails closed: starting here would create the wrong-domain services
    this handover exists to prevent.
    """
    if parent_handoff or updater_telemetry or not persist_services or disabled_services:
        return None
    from shared.config import settings

    if not settings.general.start_gui_handover:
        return None
    from shared.os_cron import os_jobs_enabled

    if not os_jobs_enabled():
        # No platform-scheduler job exists to hand over to. The test suite pins
        # this off wholesale, so no test can kick a real job by accident.
        return None
    if _rehomeable_domain(roles) is None:
        return None

    from cli.commands._session_lifecycle import _launch_roster
    from shared.disabled_services import resolve_launch_skip

    launch_skip = resolve_launch_skip(set(disabled_services), persist=persist_services)

    # Same pre-bind gate as the normal path: a health port another unit answers
    # on stops the whole bring-up, and the GUI job would refuse identically —
    # refuse here instead of kicking a doomed run.
    from cli.commands.start import _refuse_occupied_health_ports

    rc = _refuse_occupied_health_ports(_launch_roster(roles, launch_skip))
    if rc != 0:
        return rc

    return StartDelegation(
        partial(
            _launch_in_gui_domain, roles, launch_skip=launch_skip, readiness_gate=readiness_gate
        )
    )


def _launch_in_gui_domain(
    roles: MachineRoles, *, launch_skip: set[str], readiness_gate: bool
) -> int:
    """Kick and observe without owning the child's lifecycle lock or hold."""
    from shared.os_autostart import ensure_via_gui_domain

    t0 = time.time()
    try:
        ok, detail = ensure_via_gui_domain()
    except Exception as exc:  # a start must not crash on its own handover
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        print(
            f"  ! GUI-domain handover unavailable ({detail}); startup refused. "
            "Repair the GUI-domain job and retry ava start.",
            file=sys.stderr,
        )
        return 1
    print(
        "\n→ this start chain is outside the macOS GUI login session; handed the bring-up "
        f"to the cluster's GUI-domain job — {detail}"
    )
    return _observe_gui_domain_start(
        roles, launch_skip=launch_skip, readiness_gate=readiness_gate, t0=t0
    )


def _observe_gui_domain_start(
    roles: MachineRoles, *, launch_skip: set[str], readiness_gate: bool, t0: float
) -> int:
    """Wait for the handed-over bring-up and answer with the readiness verdict.

    Replicates the normal start's readiness contract — the wait, the unready
    and non-critical reporting, the alert reconciliation, the status snapshot
    and the readiness waiver — while everything only the launching process can
    own stays with the GUI-domain job: the serving generation, the running-sha
    bookmark, the launch-failure record and the resume finalization. Sessions
    the job could not launch therefore reach the operator through the job's own
    log (`$AVA_HOME/logs/autostart.log`).
    """
    import cli.commands as _ns
    from cli.commands._probe import _probe_judges_a_fresh_launch
    from cli.commands._session_lifecycle import _launch_roster
    from cli.commands.start import _readiness_waiver

    started = _launch_roster(roles, launch_skip)
    specs = tuple(s for s in started if _probe_judges_a_fresh_launch(s))
    print(
        "\n→ waiting for the GUI-domain job's bring-up (its output: $AVA_HOME/logs/autostart.log)"
    )
    if specs and not all(_ns._has_session(session_name(s.session)) for s in specs):
        # Nothing serving yet: the job must reach its launch step before the
        # readiness wait can read absence as evidence (see `_LAUNCH_SETTLE_S`).
        if _await_launch_step(t0):
            time.sleep(_LAUNCH_SETTLE_S)
        else:
            print(
                f"  ! the GUI-domain job has not reached its launch step after "
                f"{_LAUNCH_SIGNAL_TIMEOUT_S:.0f}s (see $AVA_HOME/logs/autostart.log); "
                "judging the probes directly",
                file=sys.stderr,
            )
    wait = _ns._wait_for_services_ready(specs, timeout_s=SERVICE_READY_TIMEOUT_S)

    print("\n→ status")
    from cli.commands.status import cmd_status

    cmd_status()

    if wait.unready:
        _ns._print_unready_services(wait, SERVICE_READY_TIMEOUT_S)
    if wait.non_critical_unready:
        _ns._print_non_critical_unready_services(wait.non_critical_unready)
        _ns._notify_non_critical_unready_services(
            wait.non_critical_unready, im_enabled=readiness_gate
        )
    recovered = _ns._recovered_non_critical_specs(started, wait.non_critical_unready)
    if recovered:
        _ns._resolve_recovered_non_critical_alerts(recovered, im_enabled=readiness_gate)
    if wait.unready:
        waiver = _readiness_waiver(roles, readiness_gate=readiness_gate)
        if waiver is None:
            return SERVICES_NOT_READY_EXIT_CODE
        print(f"  · {waiver}: exiting 0 anyway", file=sys.stderr)
    return 0
