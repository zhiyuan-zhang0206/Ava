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
from cli.commands._repo import build_services, session_name
from cli.commands._retired_services import stop_retired_services
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS, pause_agents
from ops.agent_pause_probe import ops_quiescent
from shared import maintenance, start_serving
from shared.machine import machine_role
from shared.paths import run_dir
from shared.session_backend import WinprocSessionBackend, get_shell_backend
from shared.session_record import SessionRecord


def _stop_terminals(deadline: float) -> None:
    """Close this unit's terminal jobs and shells without a kill escalation."""
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
    for name in names:
        record = SessionRecord.read(run_dir() / "pty" / f"{name}.json")
        if record is None:
            raise RuntimeError(f"cannot verify terminal identity: {name}")
        shell = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not shell.live():
            raise RuntimeError(f"terminal identity changed: {name}")
        shells.append(shell)
        owner[shell.pid] = name
        for identity in capture_tree(shell) - {shell}:
            jobs.add(identity)
            owner[identity.pid] = name
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
        # session by name either way so the operator can find the survivor.
        surviving = sorted(
            {owner[identity.pid] for identity in set(shells) | jobs if identity.live()}
        )
        raise TimeoutError(
            f"terminal stop incomplete — {exc} surviving terminal processes "
            f"from sessions: {surviving}"
        ) from exc
    # Hosts finish naturally after their child exits. Their protocol deliberately
    # ignores SIGTERM, so sending signals to every host process is not a stop API.
    from cli.commands._maintenance_stop import require_no_terminals

    while True:
        try:
            require_no_terminals()
            return
        except RuntimeError:
            import time

            time.sleep(min(0.05, remaining(deadline)))


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


def _mark_stopped(holder: str, acquired_at: datetime) -> None:
    """Move the held maintenance generation to stopped (re-validating it)."""
    current = maintenance.require_operation(holder, acquired_at)
    if current.maintenance is not None and current.maintenance.phase == "stopping":
        maintenance.set_phase(holder, acquired_at, "stopped")


def _timed_phase(phases: list[tuple[str, float]], label: str, step: Callable[[], object]) -> None:
    """Run one stop phase, recording its wall time even when it raises."""
    started = time.monotonic()
    try:
        step()
    except BaseException:
        phases.append((label, time.monotonic() - started))
        raise
    phases.append((label, time.monotonic() - started))


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
    from shared.proc import hosting_supervised_session

    if hosting_supervised_session() is not None:
        raise RuntimeError("pause/stop must run outside the work it drains; use a login shell")
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
    print(
        f"[ava {'pause' if keep_terminals else 'stop'}] local services; "
        f"terminals={'retained' if keep_terminals else 'closed'}; "
        f"data plane={'retained' if keep_infra else 'stopped on gateway'}"
    )
    if not _confirm_stop(require_confirmation=require_confirmation):
        return 0
    deadline = deadline_after(timeout)
    # Per-phase accounting: a failed stop names each phase and how much of the
    # shared budget it consumed, so the operator sees WHERE the budget went
    # (agent drain vs services vs terminals) — never a bare timeout (#2045).
    phases: list[tuple[str, float]] = []

    try:
        _timed_phase(phases, "retired", lambda: stop_retired_services(remaining(deadline)))
        _timed_phase(phases, "drain", lambda: pause_agents(remaining(deadline)))
        start_serving.clear_serving()
        if announce:
            _announce_stopping()
        current = maintenance.snapshot()
        assert current is not None and current.maintenance is not None  # noqa: S101
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
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
            _timed_phase(phases, "terminals", lambda: _stop_terminals(deadline))
        if teardown_extras:
            _timed_phase(phases, "extras", lambda: _stop_extras(deadline))
        if "gateway" in roles and not keep_infra:
            _timed_phase(
                phases, "data-plane", lambda: stop_data_plane(remaining(deadline), save=True)
            )
        _mark_stopped(current.holder, current.acquired_at)
    except (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
        timing = "; ".join(f"{label} {elapsed:.1f}s" for label, elapsed in phases)
        print(
            f"Pause/stop incomplete; resources were not force-killed: {exc}. "
            f"phases: {timing or 'before the first phase'}. "
            "Retry the command, or use ava start to resume.",
            file=sys.stderr,
        )
        return 1
    return 0
