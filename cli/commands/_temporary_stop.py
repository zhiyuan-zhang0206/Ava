"""Shared native pause/stop boundary for operator commands and updates."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime

import psutil

from cli.commands._maintenance_stop import (
    OwnedProcess,
    capture_tree,
    deadline_after,
    remaining,
    stop_data_plane,
    stop_services,
    wait_for_exit,
)
from cli.commands._maintenance_stop_report import (
    StopIncompleteError,
    SurvivorInventory,
    capture_survivor,
    live_identities,
)
from cli.commands._repo import _repo_root, build_services, session_name
from cli.commands._retired_services import stop_retired_services
from ops import pty_close_notices
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS, pause_agents
from ops.agent_pause_probe import ops_quiescent
from shared import maintenance, start_serving
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from shared.lifecycle_status import begin, finish, phase, status_path
from shared.machine import MachineRoles, machine_name, machine_role
from shared.paths import run_dir
from shared.session_backend import WinprocSessionBackend, get_shell_backend
from shared.session_record import SessionRecord


def _stop_terminals(deadline: float, operation: str, acquired_at: datetime) -> None:
    """Close this unit's terminal jobs and shells without a kill escalation.

    Busy sessions verified closed leave a durable closure notice for their
    owner agent (issue #2044): the gateway and ops server are already down by
    now, so the notice is delivered at the next ops-daemon startup.
    """
    backend = get_shell_backend()
    names = backend.list_sessions()
    if isinstance(backend, WinprocSessionBackend):
        from cli.commands._maintenance_stop import _TERMINAL_NAME

        names = [name for name in names if _TERMINAL_NAME.match(name)]
        stop_services(remaining(deadline), keep_terminals=True, selected=frozenset(names))
        return
    # Capture identities before signalling anything.
    shells: list[OwnedProcess] = []
    jobs: set[OwnedProcess] = set()
    owner: dict[int, str] = {}
    by_name: dict[str, OwnedProcess] = {}
    for name in names:
        record = SessionRecord.read(run_dir() / "pty" / f"{name}.json")
        if record is None:
            raise RuntimeError(f"cannot verify terminal identity: {name}")
        shell = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not shell.live():
            raise RuntimeError(f"terminal identity changed: {name}")
        shells.append(shell)
        by_name[name] = shell
        owner[shell.pid] = name
        for identity in capture_tree(shell) - {shell}:
            jobs.add(identity)
            owner[identity.pid] = name
    busy = {owner[identity.pid]: by_name[owner[identity.pid]] for identity in jobs}
    # Stop the spawners FIRST: an interactive shell's own SIGHUP makes bash
    # exit (re-sending HUP to its jobs), so a loop that restarts its job cannot
    # keep producing new descendants during the wait — the 2026-09-09 field
    # evidence showed a restart loop outliving every interrupt aimed at its
    # current job (#2045). Jobs get their graceful SIGTERM right after.
    for shell in shells:
        if shell.live():
            psutil.Process(shell.pid).send_signal(signal.SIGHUP)
    for process in jobs:
        if process.live():
            psutil.Process(process.pid).send_signal(signal.SIGTERM)
    try:
        wait_for_exit(set(shells) | jobs, deadline)
    except TimeoutError as exc:
        # A shell that HUP'd out may have dropped its record while a
        # signal-ignoring job survives as an orphan — report the owning
        # session by name and each survivor's identity (issue #2162) so the
        # operator can find and judge the exact process.
        live = live_identities(set(shells) | jobs)
        survivors = [
            capture_survivor(
                identity,
                service=owner.get(identity.pid),
                role="terminal" if identity in shells else "job",
            )
            for identity in live
        ]
        surviving = sorted({owner[identity.pid] for identity in live})
        raise StopIncompleteError(
            f"terminal stop incomplete — {exc} surviving terminal processes "
            f"from sessions: {surviving}\n"
            f"{SurvivorInventory(survivors=survivors, groups=[]).render(stage='terminals')}",
            stage="terminals",
            survivors=[survivor.payload() for survivor in survivors],
        ) from exc
    # Hosts finish naturally after their child exits. Their protocol deliberately
    # ignores SIGTERM, so sending signals to every host process is not a stop API.
    from cli.commands._maintenance_stop import require_no_terminals

    while True:
        try:
            require_no_terminals()
            break
        except RuntimeError:
            time.sleep(min(0.05, remaining(deadline)))
    _record_close_notices(busy, operation, acquired_at)


def _record_close_notices(
    busy: dict[str, OwnedProcess], operation: str, acquired_at: datetime
) -> None:
    """Durably record one closure notice per busy session verified closed.

    Only sessions whose exact process identity is gone reach this point; an
    idle session, a timed-out stop, or a Windows unit records nothing. A write
    failure is loud but never fails the stop — the resources are already
    closed and retrying the whole stop would not restore them.
    """
    for name, shell in busy.items():
        if shell.starttime is not None:
            birth = f"starttime:{shell.starttime}"
        else:
            birth = f"birth:{shell.birth!r}"
        try:
            pty_close_notices.record_close(
                machine=machine_name(),
                name=name,
                shell_pid=shell.pid,
                shell_birth=birth,
                operation=operation,
                acquired_at=acquired_at,
            )
        except Exception as exc:
            # The side-channel notice must never fail a stop whose resources
            # are already closed; stay loud so the gap is visible either way.
            print(
                f"closure notice for session {name!r} could not be recorded: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )


def _stop_browser(deadline: float) -> None:
    from services.browser.orphan import find_cluster_chrome

    trees: set[OwnedProcess] = set()
    for pid in find_cluster_chrome():
        try:
            identity = OwnedProcess.capture(psutil.Process(pid))
            trees.update(capture_tree(identity))
            if identity.live():
                psutil.Process(pid).send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            continue
    wait_for_exit(trees, deadline)
    if find_cluster_chrome():
        raise RuntimeError("this unit's browser appeared during stop")


def _stop_extras(deadline: float) -> None:
    from cli.commands._stop_extras import (
        stop_gate_service,
        stop_lgtm_services,
        stop_permissions_helper,
    )

    stop_gate_service(timeout_s=remaining(deadline))
    stop_permissions_helper(timeout_s=remaining(deadline))
    stop_lgtm_services(timeout_s=remaining(deadline))


# The compensating `ava start` gets its own budget: the failed stop already spent
# the shared deadline, and this restore is what the operator is waiting on. 600s
# matches `UV_SYNC_TIMEOUT_S`, the longest single step a start can legitimately
# run on its own (a source-integrity `uv sync`), so a compensation cut off at
# this bound is wedged, not slow. Deliberately a local constant rather than a
# `PAUSE_TIMEOUT_SECONDS` reuse: the two bound different jobs.
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
    phases: list[tuple[str, float]], *, data_plane_stopped: bool, preserved: frozenset[str]
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
    if data_plane_stopped or "data-plane" not in {label for label, _ in phases}:
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
        f"Pause/stop incomplete; resources were not force-killed: {exc}. "
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
    current = maintenance.require_operation(holder, acquired_at)
    if current.maintenance is not None and current.maintenance.phase == "stopping":
        maintenance.set_phase(holder, acquired_at, "stopped")


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
    """Drain via normal restart, then stop selected resources; never force."""
    from cli.commands.stop import _announce_stopping, _confirm_stop

    os.environ.pop("AVA_HOME_OVERRIDE", None)
    from shared.proc import hosting_exec_domain, hosting_supervised_session

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
    roles, selected, preserved = _stop_plan(
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
    data_plane_stopped = False

    try:
        _timed_phase(phases, "retired", lambda: stop_retired_services(remaining(deadline)))
        # Task #3270: an operator's own stop/pause binds the hold to this
        # command's shepherding process; daemon-driven pauses stay unbound.
        from shared.hold_driver import mint_driver

        _timed_phase(
            phases, "drain", lambda: pause_agents(remaining(deadline), driver=mint_driver())
        )
        start_serving.clear_serving()
        if announce:
            _announce_stopping()
        current = maintenance.snapshot()
        assert current is not None and current.maintenance is not None  # noqa: S101
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        holder, acquired_at = current.holder, current.acquired_at
        if current.maintenance.phase == "drained":
            from shared.host_deploy_state import set_posture

            # A failed posture write leaves the drained phase retryable. The
            # stopped phase never dials a data plane that is already offline.
            set_posture("paused")
            maintenance.set_phase(current.holder, current.acquired_at, "stopping")
        _timed_phase(phases, "quiesce", lambda: ops_quiescent(remaining(deadline)))
        _timed_phase(
            phases,
            "services",
            lambda: stop_services(remaining(deadline), keep_terminals=True, selected=selected),
        )
        if not keep_browser and "browser" not in preserved:
            _timed_phase(phases, "browser", lambda: _stop_browser(deadline))
        if not keep_terminals:
            _timed_phase(
                phases,
                "terminals",
                lambda: _stop_terminals(deadline, holder, acquired_at),
            )
        if teardown_extras:
            _timed_phase(phases, "extras", lambda: _stop_extras(deadline))
        if "gateway" in roles and not keep_infra:
            _timed_phase(
                phases, "data-plane", lambda: stop_data_plane(remaining(deadline), save=True)
            )
            data_plane_stopped = True
        _mark_stopped(current.holder, current.acquired_at)
    except (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
        _report_incomplete(
            exc,
            phases,
            owns_journal=owns_journal,
            compensated=_compensate_data_plane_failure(
                phases, data_plane_stopped=data_plane_stopped, preserved=preserved
            ),
        )
        return 1
    if owns_journal:
        finish(0)
    return 0
