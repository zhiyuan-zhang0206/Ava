"""Shared native pause/stop boundary for operator commands and updates."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

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
    # Capture identities before signalling anything. Stop jobs first so their
    # finally blocks can run while their shell and PTY host are still present.
    shells: list[OwnedProcess] = []
    jobs: set[OwnedProcess] = set()
    for name in names:
        record = SessionRecord.read(run_dir() / "pty" / f"{name}.json")
        if record is None:
            raise RuntimeError(f"cannot verify terminal identity: {name}")
        shell = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not shell.live():
            raise RuntimeError(f"terminal identity changed: {name}")
        shells.append(shell)
        jobs.update(capture_tree(shell) - {shell})
    for process in jobs:
        if process.live():
            psutil.Process(process.pid).send_signal(signal.SIGTERM)
    wait_for_exit(jobs, deadline)
    for shell in shells:
        if shell.live():
            psutil.Process(shell.pid).send_signal(signal.SIGHUP)
    wait_for_exit(set(shells), deadline)
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
    try:
        stop_retired_services(remaining(deadline))
        pause_agents(remaining(deadline))
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
        ops_quiescent(remaining(deadline))
        stop_services(remaining(deadline), keep_terminals=True, selected=selected)
        if not keep_browser and "browser" not in preserved:
            _stop_browser(deadline)
        if not keep_terminals:
            _stop_terminals(deadline)
        if teardown_extras:
            _stop_extras(deadline)
        if "gateway" in roles and not keep_infra:
            stop_data_plane(remaining(deadline), save=True)
        holder, at = current.holder, current.acquired_at
        current = maintenance.require_operation(holder, at)
        if current.maintenance is not None and current.maintenance.phase == "stopping":
            maintenance.set_phase(holder, at, "stopped")
    except (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"Pause/stop incomplete; resources were not force-killed: {exc}. "
            "Retry the command, or use ava start to resume.",
            file=sys.stderr,
        )
        return 1
    return 0
