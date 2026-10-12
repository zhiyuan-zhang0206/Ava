"""Restart, stop, and stop announcement commands; split from tests/components/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import subprocess as subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import cli.commands._repo as _repo_commands
import cli.commands.lifecycle._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
import cli.commands.lifecycle.start as _start_commands
import cli.commands.lifecycle.stop as _stop_commands
from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.deploy.lifecycle.start_serving import RootBirth
from base.sessions.pty.paths import SERVICE_UNIT
from base.telemetry import EventPipeline
from cli.commands.lifecycle.stop import _force_stop
from cli.commands.lifecycle.tests.identity.restart_inputs import (
    ignore_retained_children as _ignoring_retention,
)
from cli.commands.lifecycle.tests.identity.restart_inputs import (
    restart_command_environment as restart_command_environment,
)
from cli.commands.lifecycle.tests.identity.restart_inputs import (
    root_stop_boundary as root_stop_boundary,
)
from cli.database.runtime import CommandRuntime
from cli.tests._commands_helpers import (
    _FakeResponse,
    _FakeResult,
    _git_aware,
    _patch_gateway_http,
)
from cli.tests._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_real_reap_cluster_chrome = _stop_commands._reap_cluster_chrome


# ─── restart ─────────────────────────────────────────────────────────────────


def test_cmd_restart_succeeds_non_interactively(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """cmd_restart succeeds in a non-interactive context (the detached-updater path) —
    like cmd_start, it needs no tty."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_stop_commands, "_do_stop", MagicMock(return_value=0))

    def _apply_migrations(
        *, database_factory: DatabaseFactory, machine_name: Callable[[], str]
    ) -> list[str]:
        assert database_factory is operator_database
        assert machine_name == command_runtime.machine.name
        return []

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_migrations_apply", _apply_migrations)
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == 0


def test_cmd_restart_calls_stop_then_start(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """cmd_restart invokes stop (require_confirmation=False) then _cmd_start_body in order."""

    order: list[str] = []

    def fake_do_stop(
        _repo,
        *,
        require_confirmation,
        keep_infra=False,
        keep_browser=True,
        preserve_sessions=frozenset(),
        teardown_extras=True,
        force=False,
        retained_children: object = None,
        database_factory: Callable[[], Any],
        producer: Callable[[], EventPipeline],
        machine: MachineIdentity,
    ) -> int:
        assert database_factory is operator_database
        assert producer is operator_pipeline
        assert machine is command_runtime.machine
        assert require_confirmation is False, "cmd_restart must skip stdin confirmation"
        assert force is False
        assert keep_browser is True and teardown_extras is False, (
            "a restart keeps the browser and Gate/helper/LGTM"
        )
        assert preserve_sessions == frozenset(), "a restart closes the terminals as a stop does"
        assert keep_infra is True, (
            "an internal restart must keep the shared pg/redis up — stopping the "
            "data plane kills the orchestrator's DB polling mid-rollout"
        )
        order.append("stop")
        return 0

    def fake_cmd_start_body(
        operation: object,
        database_factory: Callable[[], Any],
        machine: MachineIdentity,
        **kwargs: object,
    ) -> int:
        assert operation is None
        assert database_factory is operator_database
        assert machine is command_runtime.machine
        assert kwargs["producer"] is operator_pipeline
        from cli.start_runtime import StartRuntime

        assert kwargs["persist_services"] is False
        assert isinstance(kwargs["runtime"], StartRuntime)
        order.append("start")
        return 0

    monkeypatch.setattr(_stop_commands, "_do_stop", fake_do_stop)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_start_commands, "_cmd_start_body", fake_cmd_start_body)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert order == ["stop", "start"]
    assert rc == 0


def test_cmd_restart_finishes_the_journal_only_when_it_owns_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """An outer operation's still-running journal is never closed by the nested
    restart — the owns_journal guard _temporary_stop keeps (task #2898)."""
    from base.deploy.lifecycle import status_journal

    # Real phases create an implicit journal while begin/finish are stubbed.
    # Keep that unfinished record out of later tests' journal ownership.
    monkeypatch.setenv("AVA_HOME", str(tmp_path))

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 0

    monkeypatch.setattr(_repo_commands, "_preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]

    def start_result(
        operation: object,
        database_factory: Callable[[], Any],
        machine: MachineIdentity,
        **kwargs: object,
    ) -> int:
        assert database_factory is operator_database
        assert kwargs["producer"] is operator_pipeline
        return 0

    monkeypatch.setattr(_start_commands, "_cmd_start_body", start_result)

    finished: list[int] = []
    monkeypatch.setattr(
        status_journal,
        "finish",
        lambda rc, **_kwargs: finished.append(rc),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        status_journal,
        "begin",
        lambda _operation, **_kwargs: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert (
        _stop_commands.cmd_restart(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert finished == []  # the outer operation still owns the journal

    monkeypatch.setattr(
        status_journal,
        "begin",
        lambda _operation, **_kwargs: True,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert (
        _stop_commands.cmd_restart(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert finished == [0]


def test_cmd_restart_short_circuits_on_stop_failure(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """If stop returns non-zero, cmd_restart propagates without calling start."""

    start_called: list[bool] = []

    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_commands,
        "_cmd_start_body",
        lambda _operation, _database_factory, _machine, **_kw: start_called.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == 1
    assert start_called == [], "start must not run when stop fails"


def test_cmd_restart_aborts_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """When preflight probes fail, cmd_restart aborts without stopping — and says so
    with its OWN exit code, since "nothing was stopped, host still serving" is what
    the detached updater must not run `ava start` over."""
    from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE

    stopped: list[bool] = []
    start_called: list[bool] = []

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 1

    monkeypatch.setattr(
        _repo_commands,
        "_preflight_probes",
        preflight,  # pyright: ignore[reportUnknownArgumentType]
    )  # simulate failure  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_kw: stopped.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_commands,
        "_cmd_start_body",
        lambda _operation, _database_factory, _machine, **_kw: start_called.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]

    def release_pause(*, database_factory: DatabaseFactory, machine: MachineIdentity) -> None:
        assert database_factory is operator_database
        assert machine is command_runtime.machine

    monkeypatch.setattr(_stop_commands, "_release_self_heal_pause", release_pause)

    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == RESTART_DECLINED_EXIT_CODE, "preflight failure must propagate non-zero"
    assert stopped == [], "must not stop services when preflight fails"
    assert start_called == [], "must not start when preflight fails"


def test_cmd_restart_aborts_when_start_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Task #3165: a start-readiness refusal (private-tree roots, ports, venv
    entry points, migrations) stops nothing — the same decline contract as the
    probes gate, with stop and start neither run. The gate is called with
    `check_launcher=False`: this start is in-process and never execs
    `.venv/bin/ava`."""
    from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE

    stopped: list[bool] = []
    start_called: list[bool] = []
    gate_calls: list[dict[str, object]] = []

    def _gate(_repo: Path, **kwargs: object) -> int:
        gate_calls.append(kwargs)
        return 1

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 0

    monkeypatch.setattr(_repo_commands, "_preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_start_readiness_preflight_commands, "preflight_start_readiness", _gate)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_kw: stopped.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_commands,
        "_cmd_start_body",
        lambda _operation, _database_factory, _machine, **_kw: start_called.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]

    def release_pause(*, database_factory: DatabaseFactory, machine: MachineIdentity) -> None:
        assert database_factory is operator_database
        assert machine is command_runtime.machine

    monkeypatch.setattr(_stop_commands, "_release_self_heal_pause", release_pause)

    rc = _stop_commands.cmd_restart(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )

    assert rc == RESTART_DECLINED_EXIT_CODE, "a readiness refusal must decline, not fail"
    assert stopped == [], "must not stop services when the readiness gate refuses"
    assert start_called == [], "must not start after a declined restart"
    from cli.start_runtime import StartRuntime

    assert len(gate_calls) == 1
    assert gate_calls[0]["check_launcher"] is False
    assert isinstance(gate_calls[0]["runtime"], StartRuntime)


# ─── stop (stdin confirmation) ────────────────────────────────────────────────────────


def test_stop_aborts_on_no(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """stdin input not y → abort, no kill / down commands called."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_kwargs):
        raise AssertionError(f"subprocess.run should not be called: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_stop(
        force=True,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    assert rc == 0


def test_stop_proceeds_on_yes(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A confirmed force stop stops the services but keeps the pty-sessions service,
    closes the terminals through it, then stops the service, and only then the
    private data plane."""
    events: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(
        _root_driver_commands,
        "stop_root_service_tree",
        lambda **kw: events.append(  # pyright: ignore[reportUnknownArgumentType] — untyped test double
            "root+terminal-service" if SERVICE_UNIT in kw["preserve"] else "root"
        ),
    )
    monkeypatch.setattr(
        "cli.commands.lifecycle.stop.force_close_terminals", lambda: events.append("terminals")
    )
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: events.append("infra") or 0),
    )
    assert (
        _stop_commands.cmd_stop(
            force=True,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
        == 0
    )
    assert events == ["root+terminal-service", "terminals", "root", "infra"]


def test_stop_revokes_serving_before_stopping_root(
    serving_root: RootBirth,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.deploy.lifecycle import start_serving

    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True
    observed: list[bool] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "stop_root_service_tree",
        lambda **_kw: observed.append(start_serving.is_serving()),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: 0),
    )
    assert (
        _force_stop(
            tmp_path,
            require_confirmation=False,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
        == 0
    )
    assert observed == [False, False], "serving is revoked before either root stop"


def test_do_stop_keep_infra_skips_infra_teardown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "stop_root_service_tree",
        lambda **_kw: events.append("root"),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: events.append("infra") or 0),
    )
    assert (
        _force_stop(
            tmp_path,
            require_confirmation=False,
            keep_infra=True,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
        == 0
    )
    assert events == ["root", "root"], "the services, then the pty-sessions service"


def test_do_stop_keeps_browser_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    calls: list[dict[str, object]] = []
    reaps: list[int] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "stop_root_service_tree",
        lambda **kw: calls.append(kw),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(_stop_commands, "_reap_cluster_chrome", lambda: reaps.append(1))
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: 0),
    )
    assert (
        _force_stop(
            tmp_path,
            require_confirmation=False,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
        == 0
    )
    assert calls == [
        {"preserve": frozenset({"browser", SERVICE_UNIT}), "force": True},
        {"preserve": frozenset({"browser"}), "force": True},
    ], "the pty-sessions service outlives the services step and is stopped after the terminals"
    assert reaps == []


def test_force_stop_keeping_the_pty_sessions_service_closes_no_terminals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """`--keep-service pty-sessions` keeps the terminals it holds: one services step,
    no terminal closure, no second stop of the service."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "stop_root_service_tree",
        lambda **kw: calls.append(kw),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )

    def closed() -> None:
        raise AssertionError("a kept service's terminals must not be closed")

    monkeypatch.setattr("cli.commands.lifecycle.stop.force_close_terminals", closed)
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: 0),
    )
    assert (
        _force_stop(
            tmp_path,
            require_confirmation=False,
            preserve_sessions=frozenset({SERVICE_UNIT}),
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
        == 0
    )
    assert calls == [{"preserve": frozenset({"browser", SERVICE_UNIT}), "force": True}]


def test_do_stop_stop_browser_kills_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    events: list[str] = []

    def stop_root(**kwargs: object) -> None:
        assert kwargs["force"] is True
        events.append("root+terminal-service" if SERVICE_UNIT in kwargs["preserve"] else "root")  # pyright: ignore[reportOperatorIssue]

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", stop_root)
    monkeypatch.setattr(_stop_commands, "_reap_cluster_chrome", lambda: events.append("browser"))
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: events.append("infra") or 0),
    )
    assert (
        _force_stop(
            tmp_path,
            require_confirmation=False,
            keep_browser=False,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
        == 0
    )
    assert events == ["root+terminal-service", "browser", "root", "infra"]


def test_reap_cluster_chrome_reports_pids_and_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The CLI seam: report what was reaped, stay silent when there was nothing,
    and never let a sweep failure fail the teardown around it."""

    monkeypatch.setattr("services.desktop.browser.orphan.reap_cluster_chrome", lambda: [4242])
    _real_reap_cluster_chrome()
    assert "4242" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr("services.desktop.browser.orphan.reap_cluster_chrome", list)
    _real_reap_cluster_chrome()
    assert capsys.readouterr().out == "", "nothing to reap prints nothing"  # pyright: ignore[reportUnknownMemberType]

    def _boom() -> list[int]:
        raise RuntimeError("process table unavailable")

    monkeypatch.setattr("services.desktop.browser.orphan.reap_cluster_chrome", _boom)
    _real_reap_cluster_chrome()  # must not raise
    assert "could not sweep" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_cmd_stop_stop_browser_flag_threads_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """`cmd_stop(stop_browser=...)` maps to `_do_stop(keep_browser=not stop_browser)`."""
    from cli.commands.lifecycle import stop as _stop_mod

    seen: dict[str, object] = {}

    def fake_do_stop(_repo, *, keep_browser=True, **_kw) -> int:
        seen["keep_browser"] = keep_browser
        return 0

    monkeypatch.setattr(_stop_mod, "_do_stop", fake_do_stop)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_mod, "_repo_root", lambda: tmp_path)

    _stop_mod.cmd_stop(
        require_confirmation=False,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    assert seen["keep_browser"] is False, "default cmd_stop closes the browser"
    _stop_mod.cmd_stop(
        require_confirmation=False,
        stop_browser=True,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    assert seen["keep_browser"] is False, "stop_browser=True takes the browser down"


# ─── gateway-backed CLI paths (stop announce) ──────────────────────────────
def _patch_stop_teardown(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Run force stop with a private root boundary and recorded storage teardown."""

    def roles(*, machine: MachineIdentity) -> frozenset[str]:
        del machine
        return frozenset({"gateway", "agent-runner"})

    monkeypatch.setattr(_repo_commands, "_roles_or_none", roles)
    monkeypatch.setattr("cli.commands.lifecycle.stop._repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.stop_cluster_instance",
        _ignoring_retention(lambda: events.append("infra") or 0),
    )


def test_cmd_stop_announces_stopping_after_confirm_before_teardown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A confirmed `ava stop` best-effort POSTs
    /api/cluster/stopping?machine=<self>&home=<self-home> before the local
    teardown (so the cluster view shows 'stopped', not 'offline'). `home`
    identifies THIS unit so a co-located peer keeps its caps."""
    from base.paths import ava_home

    _patch_gateway_http(monkeypatch)
    command_runtime.machine.set(name="test-host")
    events: list[str] = []
    calls: list[tuple[str, dict]] = []

    def _fake_post(url, **kwargs):
        events.append("announce")
        calls.append((url, kwargs))  # pyright: ignore[reportUnknownMemberType]
        return _FakeResponse({"machine": "test-host"})

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events)

    rc = _stop_commands.cmd_stop(
        require_confirmation=False,
        force=True,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    assert rc == 0
    assert calls[0][0] == "http://gw:8000/api/cluster/stopping"
    assert calls[0][1]["params"] == {"machine": "test-host", "home": str(ava_home())}
    assert events == ["announce", "infra"]  # announced first, then teardown ran


def test_cmd_stop_aborted_confirm_does_not_announce(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Declining the confirm prompt must leave the roster untouched: the stopping
    announce stamps `machines.stopped_at`, and only the next `ava start` clears
    it — an announce fired before the gate would mark a running host 'stopped'."""
    _patch_gateway_http(monkeypatch)
    command_runtime.machine.set(name="test-host")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    monkeypatch.setattr("httpx.post", lambda *_a, **_kw: events.append("announce"))  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events)

    rc = _stop_commands.cmd_stop(
        force=True,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    assert rc == 0
    assert events == []  # no announce, no teardown
    assert "aborted" in capsys.readouterr().out


def test_cmd_stop_proceeds_when_announce_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """If the stopping announce can't reach the gateway, `ava stop` logs and still
    tears down — the announce is best-effort, never a blocker."""
    _patch_gateway_http(monkeypatch)
    command_runtime.machine.set(name="runner-a")

    def _boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.post", _boom)  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    _patch_stop_teardown(monkeypatch, events)

    rc = _stop_commands.cmd_stop(
        require_confirmation=False,
        force=True,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
    )
    assert rc == 0
    assert events == ["infra"]  # teardown still ran
    assert "could not announce shutdown" in capsys.readouterr().out
