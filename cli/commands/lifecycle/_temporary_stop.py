"""Shared native pause/stop boundary for operator commands and updates."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psutil

from base.agents.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from base.cluster.machine import MachineRoles, machine_role
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.status_journal import begin, finish, phase, status_path
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.native_process.ownership import retain_processes
from cli.commands._repo import _repo_root, build_services, session_name
from cli.commands.lifecycle.service_stop import (
    OwnedProcess,
    capture_tree,
    close_terminals,
    deadline_after,
    remaining,
    stop_data_plane,
    wait_for_exit,
)
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS, pause_agents
from ops.agent_pause.probe import ops_quiescent


def _stop_browser(deadline: float) -> None:
    from services.browser.orphan import find_cluster_chrome

    trees: set[OwnedProcess] = set()
    for pid in find_cluster_chrome():
        try:
            identity = OwnedProcess.capture(psutil.Process(pid))
            retain_processes(trees, capture_tree(identity))
            if identity.live():
                identity.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            continue
    wait_for_exit(trees, deadline)
    if find_cluster_chrome():
        raise RuntimeError("this unit's browser appeared during stop")


def _stop_extras(deadline: float) -> None:
    from cli.commands.lifecycle._stop_extras import stop_permissions_helper

    stop_permissions_helper(timeout_s=remaining(deadline))


# The compensating `ava start` gets its own budget: the failed stop already spent
# the shared deadline, and this restore is what the operator is waiting on. 600s
# covers the longest single step a start can legitimately run on its own (a
# source-integrity `uv sync`), so a compensation cut off at this bound is wedged,
# not slow. Deliberately a local constant rather than a `PAUSE_TIMEOUT_SECONDS`
# reuse: the two bound different jobs.
_COMPENSATION_TIMEOUT_S = 600.0


def _compensate_services_restore(preserved: frozenset[str]) -> bool:
    """Restore this unit after a failed data-plane stop (issue #2307).

    The 2026-09-12 incident: the services phase stopped every service, then the
    data-plane stop could not complete — a pooler waiting for the paused
    runners' client connections — and the unit stayed dark until an operator
    brought it back by hand. A fail-before-stop cannot close this shape: the
    failures that reach here happen inside the data-plane stop (a hang is only
    observable by attempting the stop), so the only compensation left is
    afterwards. It is the operator's own recovery, a plain `ava start`, run
    automatically and loudly: `ensure_pgbouncer` reads the half-shut pooler as
    missing its reachable listener and restarts it fresh (the degraded-restart
    branch), and the services stopped above come back up.

    `--persist-services` marks the child as an internal start: it must not
    rewrite the operator's durable `--disable-service` marker, and under a live
    update lease it may run while deferring credential mutation to the
    orchestration — the same contract as the rollout's own fresh `ava start`.
    The preserved sessions ride through as transient skips: they were
    deliberately left running and must not be bounced.

    Returns True only when the child exited 0, its readiness gate verifying the
    launched services. Exit `SERVICES_NOT_READY_EXIT_CODE` means the unit is up
    but incomplete; every other outcome — another exit code, a timeout, failure
    to launch — counts as not restored. Each outcome is printed here; the
    caller folds the verdict into the stop report.
    """
    repo = _repo_root()
    cmd = [str(repo / ".venv" / "bin" / "ava"), "start", "--persist-services"]
    for session in sorted(preserved):
        cmd += ["--disable-service", session]
    print(
        "  · data-plane stop failed after this unit's services stopped — restoring "
        "them with `ava start`",
        flush=True,
    )
    try:
        rc = subprocess.run(cmd, cwd=repo, check=False, timeout=_COMPENSATION_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        print(
            f"  ✗ compensating `ava start` did not finish within "
            f"{int(_COMPENSATION_TIMEOUT_S)}s; this unit may still be down "
            "(retry `ava start`)",
            file=sys.stderr,
            flush=True,
        )
        return False
    except OSError as exc:
        print(
            f"  ✗ compensating `ava start` could not be launched: {exc}; this unit "
            "may still be down (retry `ava start`)",
            file=sys.stderr,
            flush=True,
        )
        return False
    if rc == 0:
        print("  ✓ compensating `ava start` restored this unit's services", flush=True)
        return True
    if rc == SERVICES_NOT_READY_EXIT_CODE:
        print(
            "  ⚠ compensating `ava start` launched this unit, but at least one service "
            "did not pass its readiness probe (exit 4); retrying `ava start` is "
            "idempotent",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(
        f"  ✗ compensating `ava start` failed (exit {rc}); this unit may still be down "
        "(retry `ava start`)",
        file=sys.stderr,
        flush=True,
    )
    return False


def _compensate_data_plane_failure(
    phases: list[tuple[str, float]],
    *,
    data_plane_stopped: bool,
    preserved: frozenset[str],
    unstarted: bool = False,
) -> bool | None:
    """Run the services restore when (and only when) the data-plane phase failed.

    `_timed_phase` appends its accounting entry even when a phase fails, so a
    "data-plane" entry the phase did not complete means the data-plane phase
    itself failed (issue #2307). The services phase before it already stopped the
    sessions it selects, and the data plane is half stopped — the dark-cluster
    shape the compensation closes. A failure before the phase cannot present this
    way, and a failure after it (`_mark_stopped`) is a stop whose destructive
    work is already done: both stay report-only. Returns the `compensated`
    verdict for the stop report; None when nothing was attempted.
    """
    if unstarted or data_plane_stopped or "data-plane" not in {label for label, _ in phases}:
        return None
    try:
        return _compensate_services_restore(preserved)
    except Exception as fault:
        # The restore is best-effort; the report and the journal must survive it.
        print(
            f"  ✗ compensating `ava start` raised {type(fault).__name__}: {fault}",
            file=sys.stderr,
        )
        return False


def _report_incomplete(
    exc: BaseException,
    phases: list[tuple[str, float]],
    *,
    owns_journal: bool,
    compensated: bool | None = None,
) -> None:
    """Print the stop's real failure with its per-phase budget accounting and
    close the lifecycle journal when this stop owns it.

    The journal record carries the structured evidence too: the failing stage
    and the surviving-process inventory a `StopIncompleteError` collected, so the
    diagnosis survives the process that printed it (issue #2162). `compensated`
    records the outcome of the services restore (issue #2307): True restored,
    False attempted without a verified restore, None not attempted.
    """
    timing = "; ".join(f"{label} {elapsed:.1f}s" for label, elapsed in phases)
    if compensated is True:
        outcome = (
            "A compensating `ava start` restored this unit's services before this "
            "report; retry the command. "
        )
    elif compensated is False:
        outcome = (
            "The compensating `ava start` did not verify a complete restore; run "
            "`ava start`, then retry the command. "
        )
    else:
        outcome = "Retry the command, or use ava start to resume. "
    print(
        f"Pause/stop incomplete; services and the data plane were not force-killed: {exc}. "
        f"phases: {timing or 'before the first phase'}. "
        f"{outcome}"
        f"Status journal: {status_path()}.",
        file=sys.stderr,
    )
    if owns_journal:
        extra: dict[str, object] = {"phases": timing}
        stage = getattr(exc, "stage", None)
        survivors = getattr(exc, "survivors", None)
        if stage:
            extra["stage"] = stage
        if survivors:
            extra["survivors"] = survivors
        if compensated is not None:
            extra["compensated"] = compensated
        finish(1, error=f"{type(exc).__name__}: {exc}", extra=extra)


def _mark_stopped(holder: str, acquired_at: datetime) -> None:
    """Move the held maintenance generation to stopped (re-validating it)."""
    current = admission.require_operation(holder, acquired_at)
    if current.maintenance is not None and current.maintenance.phase == "stopping":
        admission.set_phase(holder, acquired_at, "stopped")


def _timed_phase(phases: list[tuple[str, float]], label: str, step: Callable[[], object]) -> None:
    """Run one stop phase, recording its wall time even when it raises.

    The phase name is printed BEFORE the step runs so a caller whose outer
    budget cuts the command off mid-phase still saw which stage it entered —
    and the lifecycle journal carries the same timeline durably.
    """
    started = time.monotonic()
    print(f"  · stop phase: {label}", flush=True)
    with phase(label):
        try:
            step()
        except BaseException:
            phases.append((label, time.monotonic() - started))
            raise
        phases.append((label, time.monotonic() - started))


def _stop_plan(
    *, preserve_sessions: frozenset[str], keep_browser: bool, keep_infra: bool
) -> tuple[MachineRoles, frozenset[str], frozenset[str]]:
    """Resolve this stop's service selection from the roster and roles.

    Refuses unknown preserved sessions and preserved services that need the
    data plane while it is being stopped.
    """
    roles = machine_role()
    preserved = preserve_sessions | (frozenset({"browser"}) if keep_browser else frozenset[str]())
    specs = build_services()
    known = {spec.session for spec in specs}
    unknown = preserved - known
    if unknown:
        raise ValueError(f"unknown preserved service(s): {sorted(unknown)}")
    if "gateway" in roles and not keep_infra:
        dependent = sorted(
            spec.session for spec in specs if spec.session in preserved and spec.requires_db
        )
        if dependent:
            raise ValueError(f"preserved services require --keep-infra: {dependent}")
    selected = frozenset(
        session_name(spec.session) for spec in specs if spec.session not in preserved
    )
    return roles, selected, preserved


def _services_phase_action(*, preserved: frozenset[str], deadline: float) -> Callable[[], object]:
    """Stop services through their root owner, preserving explicitly retained units."""
    import cli.commands.lifecycle.root_driver as _root_driver_commands

    return lambda: _root_driver_commands.stop_root_service_tree(
        preserve=preserved, timeout_s=remaining(deadline)
    )


def _require_unstarted_initialization() -> bool:
    """Positive first-start evidence that no application could have admitted work."""
    from base.paths import ava_home, root_manifests_path
    from cli.commands.lifecycle.root_driver import require_root_absent
    from cli.commands.lifecycle.service_stop import require_no_terminals
    from cli.start_identity import read_intent

    intent = read_intent(ava_home())
    if intent is None or intent["phase"] != "configured":
        return False
    if "gateway" not in intent["roles"]:
        return False  # A joined runner may refer to already-existing external work.
    if start_serving.state_path().exists() or root_manifests_path().exists():
        raise RuntimeError("initialization journal conflicts with application launch evidence")
    require_root_absent()
    require_no_terminals()
    return True


def _stop_initialization(
    phases: list[tuple[str, float]],
    deadline: float,
    *,
    keep_infra: bool,
    keep_browser: bool,
    teardown_extras: bool,
    notes: list[str],
    clients: list[str],
) -> None:
    """Close a proven pre-application attempt through the existing native owners."""
    _timed_phase(
        phases, "services", _services_phase_action(preserved=frozenset(), deadline=deadline)
    )
    if not keep_browser:
        _timed_phase(phases, "browser", lambda: _stop_browser(deadline))
    if teardown_extras:
        _timed_phase(phases, "extras", lambda: _stop_extras(deadline))
    if not keep_infra:
        _timed_phase(
            phases,
            "data-plane",
            lambda: stop_data_plane(remaining(deadline), save=True, notes=notes, clients=clients),
        )


def _refuse_hosted_stop(*, keep_terminals: bool) -> None:
    """A pause/stop run from inside the work it drains strands itself mid-drain."""
    from base.host.proc import hosting_exec_domain, hosting_supervised_session

    # An exec-domain leg is SIGKILLed with the call's process group as the tool
    # call returns, mid-drain (the 2026-09-12 stranding shape). Name the one
    # host that survives per verb: a pause keeps persistent terminals, a stop
    # closes them.
    if hosting_exec_domain() is not None:
        verb = "pause" if keep_terminals else "stop"
        survives = (
            "a persistent terminal session survives a pause — host it via "
            "ava.shell.run_background(...) — or a plain login shell"
            if keep_terminals
            else "a stop closes this unit's persistent terminals too — run it from a "
            "shell no ava session hosts (e.g. a plain login shell)"
        )
        raise RuntimeError(
            f"{verb} cannot run inside execute_code: the call's teardown SIGKILLs its "
            f"process group as the call returns, stranding the {verb} mid-drain; {survives}"
        )
    if hosting_supervised_session() is not None:
        raise RuntimeError("pause/stop must run outside the work it drains; use a login shell")


@dataclass
class _StopProgress:
    """What a failed stop must know about how far it got."""

    data_plane_stopped: bool = False


def _drain_and_stop(
    phases: list[tuple[str, float]],
    deadline: float,
    progress: _StopProgress,
    *,
    roles: MachineRoles,
    preserved: frozenset[str],
    keep_infra: bool,
    keep_browser: bool,
    keep_terminals: bool,
    announce: bool,
    teardown_extras: bool,
    notes: list[str],
    clients: list[str],
) -> None:
    """Drain the agents, then stop the selected resources; each phase is timed into `phases`."""
    # Task #3270: an operator's own stop/pause binds the hold to this
    # command's shepherding process; daemon-driven pauses stay unbound.
    from base.deploy.maintenance.hold_driver import mint_driver
    from cli.commands.lifecycle.stop import _announce_stopping

    _timed_phase(
        phases,
        "drain",
        lambda: pause_agents(
            Database.from_settings(),
            EventBus.from_settings(),
            remaining(deadline),
            driver=mint_driver(),
        ),
    )
    start_serving.clear_serving()
    if announce:
        _announce_stopping()
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None  # noqa: S101
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    holder, acquired_at = current.holder, current.acquired_at
    if current.maintenance.phase == "drained":
        from base.deploy.state.host_deploy_state import set_posture

        # A failed posture write leaves the drained phase retryable. The
        # stopped phase never dials a data plane that is already offline.
        set_posture(Database.from_settings(), "paused")
        admission.set_phase(current.holder, current.acquired_at, "stopping")
    _timed_phase(phases, "quiesce", lambda: ops_quiescent(remaining(deadline)))
    _timed_phase(
        phases,
        "services",
        _services_phase_action(preserved=preserved, deadline=deadline),
    )
    if not keep_browser and "browser" not in preserved:
        _timed_phase(phases, "browser", lambda: _stop_browser(deadline))
    if not keep_terminals:
        _timed_phase(
            phases,
            "terminals",
            lambda: close_terminals(deadline, holder, acquired_at, direct_db="gateway" in roles),
        )
    if teardown_extras:
        _timed_phase(phases, "extras", lambda: _stop_extras(deadline))
    if "gateway" in roles and not keep_infra:
        _timed_phase(
            phases,
            "data-plane",
            lambda: stop_data_plane(remaining(deadline), save=True, notes=notes, clients=clients),
        )
        progress.data_plane_stopped = True
    _mark_stopped(current.holder, current.acquired_at)


def stop(
    *,
    require_confirmation: bool,
    keep_infra: bool,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    keep_terminals: bool,
    announce: bool,
    teardown_extras: bool,
    timeout: float = PAUSE_TIMEOUT_SECONDS,
) -> int:
    """Drain via normal restart, then stop selected resources.

    Services and the data plane are never forced. Closing terminals (stop, not
    pause) SIGKILLs what outlives its bounded grace
    (`service_stop.close_terminals`).
    """
    from cli.commands.lifecycle.stop import _confirm_stop

    _refuse_hosted_stop(keep_terminals=keep_terminals)
    roles, _selected, preserved = _stop_plan(
        preserve_sessions=preserve_sessions, keep_browser=keep_browser, keep_infra=keep_infra
    )
    print(
        f"[ava {'pause' if keep_terminals else 'stop'}] local services; "
        f"terminals={'retained' if keep_terminals else 'closed'}; "
        f"data plane={'retained' if keep_infra else 'stopped on gateway'}"
    )
    if not _confirm_stop(require_confirmation=require_confirmation):
        return 0
    deadline = deadline_after(timeout)
    owns_journal = begin(
        "pause" if keep_terminals else "stop",
        deadline=deadline,
    )
    # Per-phase accounting: a failed stop names each phase and how much of the
    # shared budget it consumed, so the operator sees WHERE the budget went
    # (agent drain vs services vs terminals) — never a bare timeout (#2045).
    phases: list[tuple[str, float]] = []
    progress = _StopProgress()
    unstarted = False
    notes: list[str] = []  # what the report must say: a Postgres shutdown that was escalated
    clients: list[str] = []  # the pooler's clients still connected at its stop (reported only)

    try:
        unstarted = _require_unstarted_initialization()
        if unstarted:
            _stop_initialization(
                phases,
                deadline,
                keep_infra=keep_infra,
                keep_browser=keep_browser,
                teardown_extras=teardown_extras,
                notes=notes,
                clients=clients,
            )
            return _finish_stop(owns_journal=owns_journal, notes=notes, clients=clients)
        _drain_and_stop(
            phases,
            deadline,
            progress,
            roles=roles,
            preserved=preserved,
            keep_infra=keep_infra,
            keep_browser=keep_browser,
            keep_terminals=keep_terminals,
            announce=announce,
            teardown_extras=teardown_extras,
            notes=notes,
            clients=clients,
        )
    except (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
        _report_incomplete(
            exc,
            phases,
            owns_journal=owns_journal,
            compensated=_compensate_data_plane_failure(
                phases,
                data_plane_stopped=progress.data_plane_stopped,
                preserved=preserved,
                unstarted=unstarted,
            ),
        )
        return 1
    return _finish_stop(owns_journal=owns_journal, notes=notes, clients=clients)


def _finish_stop(*, owns_journal: bool, notes: list[str], clients: list[str] | None = None) -> int:
    """The stop is done; `notes` (an escalated Postgres shutdown) and `clients` (the pooler's
    clients still connected at its stop) go into the journal."""
    for note in notes:
        print(f"Stop completed with an escalation: {note}", file=sys.stderr)
    if owns_journal:
        extra: dict[str, object] = {}
        if notes:
            extra["escalations"] = notes
        if clients:
            extra["pooler_clients"] = clients
        finish(0, extra=extra or None)
    return 0
