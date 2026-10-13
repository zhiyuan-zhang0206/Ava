"""Setup diagnostics and early failure boundaries of the one start lifecycle."""

import subprocess as subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import cli.commands._repo as _repo_commands
import cli.commands._setup as _setup_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
import cli.commands.lifecycle.start as _start_commands
from base.agents.context.clients import DatabaseFactory
from base.telemetry import EventPipeline
from cli.commands._setup import collect_setup_values as _real_collect_setup_values
from cli.commands.lifecycle.tests.startup.test_start_readiness_gate import (
    hermetic_start as hermetic_start,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_fakes import _FakeResult, _git_aware
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.supervision.roster.service_spec import ServiceSpec
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_cmd_start_needs_no_tty(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """cmd_start runs without an interactive tty. The session PATH that once
    justified a tty gate is now forwarded authoritatively per session
    (base.sessions.env_forwarding.forward_env_dict), so start works from cron / systemd / a
    headless ssh, not only a terminal."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert (
        _start_commands.cmd_start(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )


def test_root_launch_uses_the_start_callers_child_owner(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    owner_at_launch: list[list[subprocess.Popen[bytes]]] = []

    def launch(
        roster: tuple[ServiceSpec, ...],
        *_args: object,
        retained_children: list[subprocess.Popen[bytes]],
        **_kwargs: object,
    ) -> _root_driver_commands.LaunchOutcome:
        owner_at_launch.append(retained_children)
        return _root_driver_commands.LaunchOutcome(roster, ())

    monkeypatch.setattr(_root_driver_commands, "_launch_service_tree", launch)
    assert (
        _start_commands.cmd_start(
            retained_children=children,
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert len(owner_at_launch) == 1
    assert owner_at_launch[0] is children


def test_cmd_start_aborts_when_schema_mismatched(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """assert_schema_current_or_die returning non-zero short-circuits cmd_start
    before register_self / session launch, so a code-vs-DB drift fails loud at start."""

    _ = tmp_path
    launch = MagicMock()
    monkeypatch.setattr(_root_driver_commands, "_launch_service_tree", launch)
    monkeypatch.setattr(_repo_commands, "assert_schema_current_or_die", lambda: 1)

    rc = _start_commands.cmd_start(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == 1
    launch.assert_not_called()
    _ = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]


def test_start_missing_capability_reports_serve_flags_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """serve-capability is the entry to the role-aware filter; when none is declared (host serves nothing)
    other fields cannot be judged for relevance, so the error lists only the capability
    declarations rather than listing all fields."""

    command_runtime.config.set_field("machine_name", "before-failed-setup")
    command_runtime.machine.reset()
    assert command_runtime.machine.name() == "before-failed-setup"
    command_runtime.config.set_field("machine_name", "")
    command_runtime.config.set_field("machine_serve_gateway", None)
    command_runtime.config.set_field("machine_serve_agent_runner", None)
    command_runtime.config.set_field("machine_serve_observability_station", None)
    command_runtime.config.set_field("memory_remote", "")
    command_runtime.config.set_field("gateway_url", "")
    from base import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "recorded setup is incomplete" in err
    assert "AVA_MACHINE_SERVE_GATEWAY" in err
    assert "AVA_MACHINE_SERVE_AGENT_RUNNER" in err
    assert "AVA_MACHINE_NAME" not in err  # no capability, so no value field is judged
    assert command_runtime.machine.name() == "before-failed-setup"
    # A start never takes identity input: a home with none declared needs `ava init`.
    assert "`ava init`" in err


def test_start_missing_agent_runner_fields_reports_agent_runner_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """capability is agent-runner, other fields missing → error lists the agent-runner's needed keys."""

    command_runtime.config.set_field("machine_name", "")
    command_runtime.config.set_field("machine_serve_gateway", None)
    command_runtime.config.set_field("machine_serve_agent_runner", True)
    command_runtime.config.set_field("memory_remote", "")
    command_runtime.config.set_field("gateway_url", "")
    from base import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_MACHINE_NAME" in err
    assert "AVA_GATEWAY_URL" in err


def test_start_missing_gateway_fields_reports_gateway_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """capability=gateway, other fields missing → error lists the gateway's needed keys."""

    command_runtime.config.set_field("machine_name", "")
    command_runtime.config.set_field("machine_serve_gateway", True)
    command_runtime.config.set_field("machine_serve_agent_runner", None)
    command_runtime.config.set_field("machine_serve_observability_station", None)
    command_runtime.config.set_field("memory_remote", "")
    command_runtime.config.set_field("gateway_url", "")
    from base import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start(
        retained_children=[],
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_MACHINE_NAME" in err
    assert "AVA_GATEWAY_URL" in err


def test_setup_field_resolves_from_settings_only_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The settings value (the home's `.env`); a validator gates it, a legacy
    `machine_name` file is not read, and resolution never writes the home (`ava init`
    owns its identity)."""
    from base import paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    checked: list[str] = []
    field = _setup_commands.SetupField(
        name="machine_name",
        env_var="AVA_MACHINE_NAME",
        hint="<name>",
        validator=checked.append,
    )

    command_runtime.config.set_field("machine_name", "")
    (home / "machine_name").write_text("from-file\n")
    assert _setup_commands.resolve_setup_field(field, config=command_runtime.config) is None
    assert sorted(p.name for p in home.iterdir()) == ["machine_name"]  # nothing written

    command_runtime.config.set_field("machine_name", " from-env ")
    assert _setup_commands.resolve_setup_field(field, config=command_runtime.config) == "from-env"
    assert checked == ["from-env"]


def test_start_refuses_capabilities_that_differ_from_the_ones_init_recorded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The capability set is fixed when a home is initialized: a start whose
    resolved capabilities differ from the intent's refuses before any effect."""
    from base import paths
    from cli import start_identity

    home = tmp_path / "recorded"
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home,
            tmp_path,
            frozenset({"agent-runner"}),
            {"AVA_MACHINE_NAME": "t", "AVA_GATEWAY_URL": "http://127.0.0.1:1"},
        )
    )
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    launch = MagicMock()
    monkeypatch.setattr(_root_driver_commands, "_launch_service_tree", launch)

    # The hermetic start resolves a gateway-only unit; the intent says agent-runner.
    assert (
        _start_commands.cmd_start(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "differ from the ones `ava init` recorded" in err
    launch.assert_not_called()


def test_start_requires_child_owner_before_entering_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    body = MagicMock(side_effect=AssertionError("lifecycle must not be entered"))
    monkeypatch.setattr(_start_commands, "cmd_start_body", body)
    with pytest.raises(ValueError, match="caller-owned PostgreSQL child retention"):
        _start_commands.cmd_start(
            database_factory=operator_database, producer=operator_pipeline, command=command_runtime
        )
    body.assert_not_called()


def test_start_refreshes_only_its_invocations_machine(
    operator_database: DatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> None:
    other = CommandRuntime.create()
    other.config.set_field("machine_name", "other-invocation")
    command_runtime.config.set_field("machine_name", "before-setup")
    command_runtime.machine.reset()
    assert command_runtime.machine.name() == "before-setup"
    assert other.machine.name() == "other-invocation"
    command_runtime.config.set_field("machine_name", "repaired-name")

    assert (
        _start_commands.cmd_start(
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )
    assert command_runtime.machine.name() == "repaired-name"
    assert other.machine.name() == "other-invocation"
