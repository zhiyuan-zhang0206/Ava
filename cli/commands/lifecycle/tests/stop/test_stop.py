"""Operator and updater stop semantics, with private real process boundaries."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import psutil
import pytest

import cli.commands._repo as _repo_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.db import Database
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenancePhase
from base.deploy.state import host_deploy_state
from base.host import proc
from base.sessions.pty import client
from base.sessions.pty.paths import SERVICE_UNIT
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.lifecycle import _stop_extras
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle._pause_resume import StartDelegation, resume_after_start
from cli.commands.lifecycle.tests.stop.stop_support import (
    Launcher,
    PtyServiceProcess,
    busy_session,
    dependencies,
    drained,
    record_root_stops,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop.stop_support import written as written
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from ops import agent_pause, pty_close_notices
from ops.cluster_status import pause
from tests.components.agent.test_maintenance import WHEN
from tests.components.agent.test_maintenance import isolate as isolate
from tests.path_scoped import pty_jobs as jobs
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_shells import new


def _restart_stop(
    *,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
    **kwargs: Any,
) -> int:
    """The stop leg of `ava restart`: no prompt; the data plane and browser stay."""
    return entry.do_stop(
        Path("/unused"),
        require_confirmation=False,
        keep_infra=True,
        keep_browser=True,
        **kwargs,
        database_factory=database_factory,
        producer=producer,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )


def test_keeping_the_pty_sessions_service_preserves_unselected_process_and_real_pty(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """`--keep-service pty-sessions` leaves the service and its live session as they are:
    the root owner is asked to preserve it, and no terminals phase runs."""
    dependencies(monkeypatch, machine=command_runtime.machine)
    root_stops = record_root_stops(monkeypatch)

    def closed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a kept service's terminals must not be closed")

    monkeypatch.setattr(command, "close_terminals", closed)
    # Bootstrap and spawned interpreters consume the raw home before Settings.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    orchestration = launch("unowned-test-process", term="ignore")
    name = "ava-agent-987-shell-1"
    assert new(name, home, {"AVA_HOME": str(home)})
    identity = pty_reaper.track_session(name)
    deadline = time.monotonic() + 5
    while psutil.Process(identity.pid).children(recursive=True):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert (
        _restart_stop(
            preserve_sessions=frozenset({SERVICE_UNIT}),
            timeout=5,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
        == 0
    )
    assert orchestration.poll() is None
    assert client.has_session(name) and identity.live()
    assert pty_service.process is not None and pty_service.process.poll() is None
    assert [call["preserve"] for call in root_stops] == [{"browser", SERVICE_UNIT}]
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.phase == MaintenancePhase.STOPPED


def test_smooth_restart_replaces_services_and_closes_shells_but_keeps_data_plane(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """`ava restart` (smooth) stops and restarts the application services, closes the
    persistent shells as `ava stop` does (a busy session's owner gets its notice) and
    leaves the browser, the permissions helper and the data plane alone, on a gateway
    host where a full stop would take each of them down."""
    import cli.commands.lifecycle.start as start_commands
    import cli.commands.lifecycle.stop as stop_commands
    from cli.commands.lifecycle import _start_readiness_preflight, service_stop

    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(command_runtime.machine, "role", lambda: frozenset({"gateway"}))
    unowned = launch("unowned-test-process", term="ignore")
    name = "ava-agent-987-shell-2056-smooth"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    events: list[str] = []

    def record_root_stop(**kwargs: object) -> None:
        # The browser is retained from the service tree and the services are replaced; the
        # pty-sessions service stays until the terminals it holds are closed, then goes too.
        assert kwargs.get("force", False) is False
        preserve = cast("frozenset[str]", kwargs["preserve"])
        assert preserve - {SERVICE_UNIT} == frozenset({"browser"})
        events.append(
            "services-stopped" if SERVICE_UNIT in preserve else "terminal-service-stopped"
        )

    def record(label: str) -> Callable[..., object]:
        return lambda *_args, **_kwargs: events.append(label)

    def record_start(
        operation: pause_owner.PauseOwnerSnapshot | None,
        _database_factory: DatabaseFactory,
        machine: MachineIdentity,
        **kwargs: object,
    ) -> int:
        assert machine is command_runtime.machine
        assert operation is None
        assert kwargs["persist_services"] is False
        events.append("services-started")
        return 0

    real_close = service_stop.close_terminals

    def close_terminals(*args: Any, **kwargs: Any) -> None:
        events.append("terminals-closed")
        real_close(*args, **kwargs)

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", record_root_stop)
    monkeypatch.setattr(command, "stop_data_plane", record("data-plane-stopped"))
    monkeypatch.setattr(command, "stop_browser", record("browser-stopped"))
    monkeypatch.setattr(command, "stop_extras", record("extras-stopped"))
    monkeypatch.setattr(command, "close_terminals", close_terminals)
    monkeypatch.setattr(command, "announce_stopping", record("announced"))
    runtime = MagicMock()
    monkeypatch.setattr(stop_commands, "restart_runtime", lambda: runtime)
    monkeypatch.setattr(stop_commands, "require_restart_runtime", lambda _runtime: None)  # pyright: ignore[reportUnknownArgumentType]

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 0

    monkeypatch.setattr(_repo_commands, "preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(start_commands, "cmd_start_body", record_start)

    assert (
        entry.cmd_restart(
            mode="smooth",
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )

    assert events == [
        "services-stopped",
        "terminals-closed",
        "terminal-service-stopped",
        "services-started",
    ]
    assert not client.has_session(name), "a restart closes the persistent shells"
    assert [notice.name for notice in written] == [name], "the busy session's owner is told"
    assert unowned.poll() is None
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert (
        current.maintenance.phase == MaintenancePhase.STOPPED
    )  # the start leg, not the stop, releases the hold


def test_root_stop_refusal_keeps_hold_without_force(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    service = launch("ava-worker", term="ignore")
    before = psutil.Process(service.pid).create_time()

    def refuse(**kwargs: object) -> None:
        assert kwargs.get("force", False) is False
        raise RuntimeError("root service did not stop")

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", refuse)
    assert (
        _restart_stop(
            timeout=0.2,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
        == 1
    )
    assert service.poll() is None
    assert psutil.Process(service.pid).create_time() == before
    assert admission.held()


def test_full_stop_closes_real_idle_terminal_after_drain(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The services phase keeps the pty-sessions service; the terminals phase closes
    the session through it; only then is the service itself stopped."""
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    root_stops: list[tuple[bool, list[str]]] = []

    def record(**kwargs: Any) -> int:
        """Whether the service was asked to stay, and the sessions it held at that moment."""
        root_stops.append(
            (SERVICE_UNIT in kwargs["preserve"], [info.name for info in client.list_sessions()])
        )
        return 0

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", record)
    for hook in ("stop_permissions_helper",):
        monkeypatch.setattr(_stop_extras, hook, lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]

    def announce(*, machine: MachineIdentity) -> None:
        assert machine is command_runtime.machine

    monkeypatch.setattr(command, "announce_stopping", announce)
    name = "ava-agent-987-shell-2"
    assert new(name, home, {"AVA_HOME": str(home)})
    pty_reaper.track_session(name)
    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=True,
            timeout=5,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    assert not client.has_session(name)
    assert root_stops == [(True, [name]), (False, [])], (
        "the service stops only after its terminals closed"
    )


def test_normal_start_releases_hold_only_after_successful_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    command_runtime: CommandRuntime,
) -> None:
    drained()
    monkeypatch.setattr(host_deploy_state, "set_posture", MagicMock())
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", lambda _db, _bus, *_a: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)

    @resume_after_start
    def start(
        operation: pause_owner.PauseOwnerSnapshot | None,
        _database_factory: DatabaseFactory,
        result: int,
    ) -> int:
        admission.require_start_allowed(operation)
        assert admission.held()
        return result

    assert start(None, operator_database, command_runtime.machine, 4) == 4
    assert admission.held()
    assert "hold released" not in capsys.readouterr().out
    assert start(None, operator_database, command_runtime.machine, 0) == 0
    assert not admission.held()
    # The start's status snapshot still read paused; the release is reported.
    assert "maintenance hold released" in capsys.readouterr().out


def test_delegated_start_leaves_authorization_and_resume_with_child(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    command_runtime: CommandRuntime,
) -> None:
    drained()
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    unpause = MagicMock()
    monkeypatch.setattr(pause, "unpause_local_cluster", unpause)

    def child() -> int:
        assert not admission.start_authorized(None)
        assert admission.held()
        return 0

    @resume_after_start
    def start(
        operation: pause_owner.PauseOwnerSnapshot | None, database_factory: DatabaseFactory
    ) -> StartDelegation:
        assert admission.start_authorized(operation)
        return StartDelegation(child)

    assert start(None, operator_database, command_runtime.machine) == 0
    assert admission.held()
    unpause.assert_not_called()


def test_only_explicit_force_enters_legacy_force_stop(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    normal, force = MagicMock(return_value=0), MagicMock(return_value=0)
    monkeypatch.setattr(command, "stop", normal)
    monkeypatch.setattr(entry, "force_stop", force)
    assert (
        entry.do_stop(
            Path("/unused"),
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    normal.assert_called_once()
    force.assert_not_called()
    assert (
        entry.cmd_stop(
            force=True,
            require_confirmation=False,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    force.assert_called_once()


def test_repeated_stop_needs_no_live_database_or_host(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    drained()
    admission.set_phase("local", WHEN, MaintenancePhase.STOPPING)
    admission.set_phase("local", WHEN, MaintenancePhase.STOPPED)
    monkeypatch.setattr(command, "pause_agents", agent_pause.pause_agents)
    monkeypatch.setattr(
        agent_pause, "host_identity", MagicMock(side_effect=AssertionError("host is down"))
    )
    monkeypatch.setattr(Database, "connect", MagicMock(side_effect=AssertionError("DB is down")))
    monkeypatch.setattr(
        host_deploy_state,
        "set_posture",
        MagicMock(side_effect=AssertionError("DB is down")),
    )
    assert (
        _restart_stop(
            timeout=1,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
        == 0
    )


def test_two_stop_start_cycles_reuse_identity_not_old_operation(
    db_url: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setattr(command, "pause_agents", agent_pause.pause_agents)
    command_runtime.machine.set(role="gateway", name="test-machine")
    monkeypatch.setattr(host_deploy_state, "set_posture", MagicMock())
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    starts = resume_after_start(lambda _operation, _database_factory: 0)
    holders: list[str | None] = []
    for _ in range(2):
        assert (
            _restart_stop(
                timeout=3,
                database_factory=operator_database,
                producer=operator_pipeline,
                command_runtime=command_runtime,
            )
            == 0
        )
        holders.append(pause_owner.read().holder)
        assert admission.held()
        assert starts(None, operator_database, command_runtime.machine) == 0
        assert not admission.held()
    assert holders[0] != holders[1]


def test_resource_stop_excludes_concurrent_start(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    command_runtime: CommandRuntime,
) -> None:
    from threading import Event, Thread

    from cli.commands.lifecycle._pause_resume import exclusive_resources

    entered, finish, start_finished = Event(), Event(), Event()

    @exclusive_resources
    def stopping() -> None:
        entered.set()
        assert finish.wait(5)

    thread = Thread(target=stopping)
    thread.start()
    assert entered.wait(5)
    start = MagicMock(return_value=0)

    def waiting_start() -> None:
        assert resume_after_start(start)(None, operator_database, command_runtime.machine) == 0
        start_finished.set()

    starter = Thread(target=waiting_start)
    starter.start()
    try:
        assert not start_finished.wait(0.2)
        start.assert_not_called()
    finally:
        finish.set()
        thread.join(5)
        starter.join(5)
    assert not thread.is_alive()
    assert not starter.is_alive()
    assert start_finished.is_set()
    start.assert_called_once()


@pytest.mark.parametrize("how", ["stop", "restart", "keep-service"])
def test_explicit_force_stops_host_and_closes_terminals_unless_their_service_is_kept(
    how: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Force stops the services with the pty-sessions service kept, then closes the
    terminals through it and stops it, with no drain and no owner notices;
    keeping the service keeps its live session."""

    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"agent-runner"})

    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_repo_commands, "roles_or_none", read_roles)

    def announce(*, machine: MachineIdentity) -> None:
        assert machine is command_runtime.machine

    monkeypatch.setattr(command, "announce_stopping", announce)
    for hook in ("stop_permissions_helper",):
        monkeypatch.setattr(_stop_extras, hook, lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        command, "pause_agents", MagicMock(side_effect=AssertionError("force fabricated a drain"))
    )
    root_calls = record_root_stops(monkeypatch)
    name = "ava-agent-987-shell-2057-force"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)
    if how == "stop":
        rc = entry.cmd_stop(
            force=True,
            require_confirmation=False,
            stop_browser=False,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
    elif how == "restart":
        rc = _restart_stop(
            force=True,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
    else:
        rc = _restart_stop(
            force=True,
            preserve_sessions=frozenset({SERVICE_UNIT}),
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
    kept = how == "keep-service"
    assert rc == 0
    assert all(call["force"] is True for call in root_calls)
    assert [SERVICE_UNIT in call["preserve"] for call in root_calls] == (
        [True] if kept else [True, False]
    )
    assert client.has_session(name) is kept
    assert written == [], "force writes no owner notices"
    assert not admission.held(), "force must not invent a durable flush receipt"


# ── a failed data-plane stop is reported, never compensated ───────────────────


def _stop_with_gateway_data_plane(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """A gateway stop whose phases are real no-ops except what a test patches."""
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setattr(command_runtime.machine, "role", lambda: frozenset({"gateway"}))


def test_stop_reports_a_data_plane_failure_without_restarting_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The 2026-09-12 shape through the real stop() flow: the services phase ran and
    the data-plane phase failed. The stop fails loudly and leaves the decision to
    the operator: no `ava start` child is launched and the journal carries no
    restore verdict."""
    _stop_with_gateway_data_plane(monkeypatch, command_runtime=command_runtime)

    def _data_plane_stop(*_args: object, **_kw: object) -> None:
        raise RuntimeError("maintenance kept its hold; processes did not exit: [2465]")

    def _no_child(*_args: object, **_kw: object) -> None:
        raise AssertionError("a failed stop must not spawn a compensating `ava start`")

    finished: list[dict[str, object]] = []

    def _begin(*_args: object, **_kw: object) -> bool:
        return True

    def _finish(_rc: int, *, error: str, extra: dict[str, object]) -> None:
        finished.append({"error": error, **extra})

    monkeypatch.setattr(command, "stop_data_plane", _data_plane_stop)
    monkeypatch.setattr(command.subprocess, "run", _no_child)
    monkeypatch.setattr(command, "begin", _begin)
    monkeypatch.setattr(command, "finish", _finish)

    rc = command.stop(
        require_confirmation=False,
        keep_infra=False,
        preserve_sessions=frozenset({"worker"}),
        keep_browser=True,
        announce=False,
        teardown_extras=False,
        timeout=1,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "Stop incomplete" in err
    assert "Retry the command, or use ava start to resume." in err
    assert "compensating" not in err
    assert len(finished) == 1 and "compensated" not in finished[0]


@pytest.mark.parametrize("leg", ["stop", "restart"])
def test_stop_refused_inside_an_exec_domain(
    monkeypatch: pytest.MonkeyPatch,
    leg: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Issue #2331: an exec-domain stop is SIGKILLed mid-drain with the call's process
    group, and a stop (a restart's stop leg too) closes this unit's persistent
    terminals — the refusal points at a shell no ava session hosts."""
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setattr(proc, "hosting_exec_domain", lambda: "agent.execution.child")

    with pytest.raises(RuntimeError, match="login shell"):
        if leg == "stop":
            entry.cmd_stop(
                require_confirmation=False,
                timeout=1,
                database_factory=operator_database,
                producer=operator_pipeline,
                machine=command_runtime.machine,
                helper=refuse_helper,
            )
        else:
            _restart_stop(
                timeout=1,
                database_factory=operator_database,
                producer=operator_pipeline,
                command_runtime=command_runtime,
            )


def test_nested_start_receives_the_exact_outer_operation_without_releasing_it(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    command_runtime: CommandRuntime,
) -> None:
    drained()
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    unpause = MagicMock()
    monkeypatch.setattr(pause, "unpause_local_cluster", unpause)
    operation = admission.authorized_start("local", WHEN)

    @resume_after_start
    def nested(
        authority: pause_owner.PauseOwnerSnapshot | None, database_factory: DatabaseFactory
    ) -> int:
        assert authority is operation
        admission.require_start_allowed(authority)
        return 0

    assert nested(operation, operator_database, command_runtime.machine) == 0
    assert admission.held()
    unpause.assert_not_called()
