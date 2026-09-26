"""Setup diagnostics and early failure boundaries of the one start lifecycle."""

import subprocess as subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cli.commands._repo as _repo_commands
import cli.commands._root_driver as _root_driver_commands
import cli.commands._setup as _setup_commands
import cli.commands.start as _start_commands
from cli.commands._setup import _collect_setup_values as _real_collect_setup_values
from shared.config import settings
from tests.cli._commands_helpers import _FakeResult, _git_aware
from tests.cli.test_start_readiness_gate import _hermetic_start as _hermetic_start


def test_cmd_start_needs_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_start runs without an interactive tty. The session PATH that once
    justified a tty gate is now forwarded authoritatively per session
    (shared.session_env.forward_env_dict), so start works from cron / systemd / a
    headless ssh, not only a terminal."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _start_commands.cmd_start() == 0


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

    rc = _start_commands.cmd_start()
    assert rc == 1
    launch.assert_not_called()
    _ = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]


def test_start_missing_capability_reports_serve_flags_only(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """serve-capability is the entry to the role-aware filter; when both are missing (host serves nothing)
    other fields cannot be judged for relevance, so the error lists only the two --serve-* flags
    rather than listing all fields."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start()
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "missing required" in err
    assert "--serve-gateway" in err
    assert "--serve-agent-runner" in err
    # When capability is not resolved, the example should give both gateway + agent-runner commands
    assert "ava start --machine-name <name> --serve-gateway " in err
    assert "ava start --machine-name <name> --serve-agent-runner " in err


def test_start_missing_agent_runner_fields_reports_agent_runner_flags(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """capability is agent-runner, other fields missing → error lists agent-runner needed flags (--gateway-url)."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", True)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start()
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "--machine-name" in err
    assert "--gateway-url" in err


def test_start_missing_gateway_fields_reports_gateway_flags(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """capability=gateway, other fields missing → error lists gateway needed flags (--gateway-url)."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", True)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _real_collect_setup_values)

    rc = _start_commands.cmd_start()
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "--machine-name" in err
    assert "--gateway-url" in err


def test_setup_field_resolves_env_then_home_file_then_arg_without_persisting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env > `$AVA_HOME/<name>` file > CLI arg; a validator gates every source,
    and a CLI arg is never written back to the home (start identity owns it)."""
    from shared import paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    checked: list[str] = []
    field = _setup_commands._SetupField(
        name="machine_name",
        cli_flag="--machine-name",
        env_var="AVA_MACHINE_NAME",
        hint="<name>",
        validator=checked.append,
    )

    monkeypatch.setattr(settings.general, "machine_name", "")
    assert _setup_commands._resolve_setup_field(field, None) is None
    assert _setup_commands._resolve_setup_field(field, "from-arg") == "from-arg"
    assert not (home / "machine_name").exists()

    (home / "machine_name").write_text(" from-file \n")
    assert _setup_commands._resolve_setup_field(field, "from-arg") == "from-file"

    monkeypatch.setattr(settings.general, "machine_name", " from-env ")
    assert _setup_commands._resolve_setup_field(field, "from-arg") == "from-env"
    assert checked == ["from-arg", "from-file", "from-env"]
