"""Shared native stop boundary for operator commands and updates."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime

import psutil

from base.agents.context.clients import DatabaseFactory
from base.cluster import session_name
from base.cluster.machine import MachineIdentity, MachineRoles
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.status_journal import begin, finish, phase, status_path
from base.deploy.maintenance import admission
from base.deploy.maintenance.state import MaintenancePhase
from base.events.live.bus import EventBus
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.sessions.pty.paths import SERVICE_UNIT
from base.telemetry import EventPipeline
from cli.commands.data_plane.stop_waits import deadline_after, remaining, wait_for_exit
from cli.commands.lifecycle.service_stop import close_terminals, stop_data_plane
from ops.agent_pause import PAUSE_TIMEOUT_SECONDS, pause_agents
from ops.agent_pause.probe import ops_quiescent
from services.supervision.roster import build_services


def stop_browser(deadline: float) -> None:
    from services.desktop.browser.orphan import find_cluster_chrome

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


def stop_extras(deadline: float) -> None:
    from cli.commands.lifecycle._stop_extras import stop_permissions_helper

    stop_permissions_helper(timeout_s=remaining(deadline))


def report_incomplete(
    exc: BaseException,
    phases: list[tuple[str, float]],
    *,
    owns_journal: bool,
) -> None:
    """Print the stop's real failure with its per-phase budget accounting and
    close the lifecycle journal when this stop owns it.

    The journal record carries the structured evidence too: the failing stage
    and the surviving-process inventory a `StopIncompleteError` collected, so the
    diagnosis survives the process that printed it (issue #2162). A failed stop
    is reported as it is: nothing is restarted behind the operator's back.
    """
    timing = "; ".join(f"{label} {elapsed:.1f}s" for label, elapsed in phases)
    print(
        f"Stop incomplete; services and the data plane were not force-killed: {exc}. "
        f"phases: {timing or 'before the first phase'}. "
        "Retry the command, or use ava start to resume. "
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
        finish(1, error=f"{type(exc).__name__}: {exc}", extra=extra)


def _mark_stopped(holder: str, acquired_at: datetime) -> None:
    """Move the held maintenance generation to stopped (re-validating it)."""
    current = admission.require_operation(holder, acquired_at)
    if current.maintenance is not None and current.maintenance.phase == MaintenancePhase.STOPPING:
        admission.set_phase(holder, acquired_at, MaintenancePhase.STOPPED)


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


def stop_plan(
    *,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    keep_infra: bool,
    machine: MachineIdentity,
) -> tuple[MachineRoles, frozenset[str], frozenset[str]]:
    """Resolve this stop's service selection from the roster and roles.

    Refuses unknown preserved sessions and preserved services that need the data
    plane while it is being stopped.
    """
    roles = machine.role()
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


def services_phase_action(*, preserved: frozenset[str], deadline: float) -> Callable[[], object]:
    """Stop services through their root owner, preserving explicitly retained units."""
    import cli.commands.lifecycle.root_driver as _root_driver_commands

    return lambda: _root_driver_commands.stop_root_service_tree(
        preserve=preserved, timeout_s=remaining(deadline)
    )


def require_unstarted_initialization() -> bool:
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
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
) -> None:
    """Close a proven pre-application attempt through the existing native owners."""
    _timed_phase(
        phases, "services", services_phase_action(preserved=frozenset(), deadline=deadline)
    )
    if not keep_browser:
        _timed_phase(phases, "browser", lambda: stop_browser(deadline))
    if teardown_extras:
        _timed_phase(phases, "extras", lambda: stop_extras(deadline))
    if not keep_infra:
        _timed_phase(
            phases,
            "data-plane",
            lambda: stop_data_plane(
                remaining(deadline),
                save=True,
                notes=notes,
                clients=clients,
                retained_children=retained_children,
                producer=producer,
            ),
        )


def _refuse_hosted_stop() -> None:
    """A stop run from inside the work it drains strands itself mid-drain."""
    from base.host.proc import hosting_exec_domain
    from base.sessions.hosting import hosting_supervised_session

    # An exec-domain leg is SIGKILLed with the call's process group as the tool
    # call returns, mid-drain (the 2026-09-12 stranding shape). A stop closes this
    # unit's persistent terminals too, so no terminal can host it either.
    if hosting_exec_domain() is not None:
        raise RuntimeError(
            "stop cannot run inside execute_code: the call's teardown SIGKILLs its "
            "process group as the call returns, stranding the stop mid-drain; a stop "
            "closes this unit's persistent terminals too — run it from a shell no ava "
            "session hosts (e.g. a plain login shell)"
        )
    if hosting_supervised_session() is not None:
        raise RuntimeError("stop must run outside the work it drains; use a login shell")


def _drain_and_stop(
    phases: list[tuple[str, float]],
    deadline: float,
    *,
    roles: MachineRoles,
    preserved: frozenset[str],
    keep_infra: bool,
    keep_browser: bool,
    announce: bool,
    teardown_extras: bool,
    notes: list[str],
    clients: list[str],
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentity,
) -> None:
    """Drain the agents, then stop the selected resources; each phase is timed into `phases`."""
    _timed_phase(
        phases,
        "drain",
        lambda: pause_agents(
            database_factory(),
            EventBus.from_settings(),
            remaining(deadline),
            local_machine=machine,
        ),
    )
    start_serving.clear_serving()
    if announce:
        announce_stopping(machine=machine)
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None  # noqa: S101
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    holder, acquired_at = current.holder, current.acquired_at
    if current.maintenance.phase == MaintenancePhase.DRAINED:
        from base.deploy.state.host_deploy_state import set_posture

        # A failed posture write leaves the drained phase retryable. The
        # stopped phase never dials a data plane that is already offline.
        set_posture(database_factory(), "paused", machine=machine.name())
        admission.set_phase(current.holder, current.acquired_at, MaintenancePhase.STOPPING)
    _timed_phase(phases, "quiesce", lambda: ops_quiescent(remaining(deadline)))
    # The pty-sessions service outlives the services phase: it closes the terminals
    # below. Keeping it (`--keep-service pty-sessions`) keeps the terminals it holds.
    keep_terminals = SERVICE_UNIT in preserved
    _timed_phase(
        phases,
        "services",
        services_phase_action(preserved=preserved | {SERVICE_UNIT}, deadline=deadline),
    )
    if not keep_browser and "browser" not in preserved:
        _timed_phase(phases, "browser", lambda: stop_browser(deadline))
    if not keep_terminals:
        _timed_phase(
            phases,
            "terminals",
            lambda: close_terminals(
                deadline,
                holder,
                acquired_at,
                direct_db="gateway" in roles,
                database_factory=database_factory,
                local_machine=machine,
            ),
        )
        # Nothing is left in the service: stop it, and the root with it when nothing else is kept.
        _timed_phase(
            phases,
            "pty-sessions",
            services_phase_action(preserved=preserved, deadline=deadline),
        )
    if teardown_extras:
        _timed_phase(phases, "extras", lambda: stop_extras(deadline))
    if "gateway" in roles and not keep_infra:
        _timed_phase(
            phases,
            "data-plane",
            lambda: stop_data_plane(
                remaining(deadline),
                save=True,
                notes=notes,
                clients=clients,
                retained_children=retained_children,
                producer=producer,
            ),
        )
    _mark_stopped(current.holder, current.acquired_at)


def stop(
    *,
    require_confirmation: bool,
    keep_infra: bool,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    announce: bool,
    teardown_extras: bool,
    timeout: float = PAUSE_TIMEOUT_SECONDS,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentity,
) -> int:
    """Drain via normal restart, then stop selected resources.

    Services and the data plane are never forced. Closing terminals (skipped
    only when the pty-sessions service itself is kept) SIGKILLs what outlives
    its bounded grace (`service_stop.close_terminals`).
    """
    _refuse_hosted_stop()
    roles, _selected, preserved = stop_plan(
        preserve_sessions=preserve_sessions,
        keep_browser=keep_browser,
        keep_infra=keep_infra,
        machine=machine,
    )
    print(
        "[ava stop] local services; "
        f"terminals={'retained' if SERVICE_UNIT in preserved else 'closed'}; "
        f"data plane={'retained' if keep_infra else 'stopped on gateway'}"
    )
    if not confirm_stop(require_confirmation=require_confirmation):
        return 0
    deadline = deadline_after(timeout)
    owns_journal = begin("stop", deadline=deadline)
    # Per-phase accounting: a failed stop names each phase and how much of the
    # shared budget it consumed, so the operator sees WHERE the budget went
    # (agent drain vs services vs terminals) — never a bare timeout (#2045).
    phases: list[tuple[str, float]] = []
    notes: list[str] = []  # what the report must say: a Postgres shutdown that was escalated
    clients: list[str] = []  # the pooler's clients still connected at its stop (reported only)

    try:
        if require_unstarted_initialization():
            _stop_initialization(
                phases,
                deadline,
                keep_infra=keep_infra,
                keep_browser=keep_browser,
                teardown_extras=teardown_extras,
                notes=notes,
                clients=clients,
                retained_children=retained_children,
                producer=producer,
            )
            return finish_stop(owns_journal=owns_journal, notes=notes, clients=clients)
        _drain_and_stop(
            phases,
            deadline,
            roles=roles,
            preserved=preserved,
            keep_infra=keep_infra,
            keep_browser=keep_browser,
            announce=announce,
            teardown_extras=teardown_extras,
            notes=notes,
            clients=clients,
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            machine=machine,
        )
    except (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
        report_incomplete(exc, phases, owns_journal=owns_journal)
        return 1
    return finish_stop(owns_journal=owns_journal, notes=notes, clients=clients)


def finish_stop(*, owns_journal: bool, notes: list[str], clients: list[str] | None = None) -> int:
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


def confirm_stop(*, require_confirmation: bool) -> bool:
    """The stdin "y/N" gate. Returns True when the stop may proceed."""
    if not require_confirmation:
        return True
    try:
        answer = input("confirm? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("aborted")
        return False
    return True


def announce_stopping(*, machine: MachineIdentity) -> None:
    """Best-effort POST `/api/cluster/stopping` before local teardown.

    Stamps the stopping unit's `stopped_at` and recomputes the composed
    `machines` row so the cluster view distinguishes an intentional `ava stop`
    from a crash. `home` identifies THIS unit (a co-located peer keeps its
    caps). Best-effort by design: if the gateway is unreachable we are stopping
    anyway, so we log and proceed rather than block the teardown.
    """
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import post as dial_post
    from base.paths import ava_home

    try:
        name = machine.name()
        home = str(ava_home())
        url = f"{gateway_api_base()}/api/cluster/stopping"
        resp = dial_post(
            url, params={"machine": name, "home": home}, timeout=5.0, headers=gateway_auth_headers()
        )
        resp.raise_for_status()
        print(f"[ava stop] announced intentional shutdown of {name!r} ({home}) to the gateway")
    except Exception as e:
        print(f"[ava stop] could not announce shutdown (proceeding anyway): {e}")
