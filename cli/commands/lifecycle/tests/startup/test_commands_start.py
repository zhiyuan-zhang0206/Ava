"""Setup diagnostics and early failure boundaries of the one start lifecycle."""

import subprocess as subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cli.commands._repo as _repo_commands
import cli.commands._setup as _setup_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
import cli.commands.lifecycle.start as _start_commands
from base.config import settings
from cli.commands._setup import _collect_setup_values as _real_collect_setup_values
from cli.commands.lifecycle.tests.startup.test_start_readiness_gate import (
    _hermetic_start as _hermetic_start,
)
from cli.tests._commands_helpers import _FakeResult, _git_aware


def test_cmd_start_needs_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert _start_commands.cmd_start(retained_children=[]) == 0


def test_cmd_start_aborts_when_schema_mismatched(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
) -> None:
    """_assert_schema_current_or_die returning non-zero short-circuits cmd_start
    before register_self / session launch, so a code-vs-DB drift fails loud at start."""

    _ = tmp_path
    launch = MagicMock()
    monkeypatch.setattr(_root_driver_commands, "_launch_service_tree", launch)
    monkeypatch.setattr(_repo_commands, "_assert_schema_current_or_die", lambda: 1)

    rc = _start_commands.cmd_start(retained_children=[])
    assert rc == 1
    launch.assert_not_called()
    _ = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]


def test_start_missing_capability_reports_serve_flags_only(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """serve-capability is the entry to the role-aware filter; when none is declared (host serves nothing)
    other fields cannot be judged for relevance, so the error lists only the capability
    declarations rather than listing all fields."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from base import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start(retained_children=[])
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "recorded setup is incomplete" in err
    assert "AVA_MACHINE_SERVE_GATEWAY" in err
    assert "AVA_MACHINE_SERVE_AGENT_RUNNER" in err
    assert "AVA_MACHINE_NAME" not in err  # no capability, so no value field is judged
    # A start never takes identity input: a home with none declared needs `ava init`.
    assert "`ava init`" in err


def test_start_missing_agent_runner_fields_reports_agent_runner_flags(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """capability is agent-runner, other fields missing → error lists the agent-runner's needed keys."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", True)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from base import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start(retained_children=[])
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_MACHINE_NAME" in err
    assert "AVA_GATEWAY_URL" in err


def test_start_missing_gateway_fields_reports_gateway_flags(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """capability=gateway, other fields missing → error lists the gateway's needed keys."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", True)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from base import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start(retained_children=[])
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_MACHINE_NAME" in err
    assert "AVA_GATEWAY_URL" in err


def test_setup_field_resolves_from_settings_only_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The settings value (the home's `.env`); a validator gates it, a legacy
    `machine_name` file is not read, and resolution never writes the home (`ava init`
    owns its identity)."""
    from base import paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    checked: list[str] = []
    field = _setup_commands._SetupField(
        name="machine_name",
        env_var="AVA_MACHINE_NAME",
        hint="<name>",
        validator=checked.append,
    )

    monkeypatch.setattr(settings.general, "machine_name", "")
    (home / "machine_name").write_text("from-file\n")
    assert _setup_commands._resolve_setup_field(field) is None
    assert sorted(p.name for p in home.iterdir()) == ["machine_name"]  # nothing written

    monkeypatch.setattr(settings.general, "machine_name", " from-env ")
    assert _setup_commands._resolve_setup_field(field) == "from-env"
    assert checked == ["from-env"]


def test_start_refuses_capabilities_that_differ_from_the_ones_init_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
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
    assert _start_commands.cmd_start(retained_children=[]) == 1
    err = capsys.readouterr().err
    assert "differ from the ones `ava init` recorded" in err
    launch.assert_not_called()


def test_start_requires_child_owner_before_entering_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = MagicMock(side_effect=AssertionError("lifecycle must not be entered"))
    monkeypatch.setattr(_start_commands, "_cmd_start_body", body)
    with pytest.raises(ValueError, match="caller-owned PostgreSQL child retention"):
        _start_commands.cmd_start()
    body.assert_not_called()
