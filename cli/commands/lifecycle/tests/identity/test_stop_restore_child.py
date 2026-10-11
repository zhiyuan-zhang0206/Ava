"""Internal start compensation retains its selected services, exits and finite budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.agents.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle.tests.stop_support import home as home
from cli.commands.lifecycle.tests.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop_support import written as written
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.components.agent.test_maintenance import isolate as isolate
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "ava").touch()
    return repo


def test_services_restore_runs_the_internal_start_with_preserved_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is exactly the internal start: `--persist-services` (never an
    operator marker rewrite), the preserved sessions as transient skips, bounded
    with its own budget."""
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(command, "_repo_root", lambda: repo)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _Completed:
        returncode = 0

    def _run(cmd: list[str], **kwargs: object) -> _Completed:
        calls.append((cmd, dict(kwargs)))
        return _Completed()

    monkeypatch.setattr(command.subprocess, "run", _run)

    assert command._compensate_services_restore(frozenset({"worker", "browser"})) is True
    assert [cmd for cmd, _ in calls] == [
        [
            str(repo / ".venv" / "bin" / "ava"),
            "start",
            "--persist-services",
            "--disable-service",
            "browser",
            "--disable-service",
            "worker",
        ]
    ]
    assert calls[0][1] == {
        "cwd": repo,
        "check": False,
        "timeout": command._COMPENSATION_TIMEOUT_S,
    }


@pytest.mark.parametrize(
    ("returncode", "expected", "message"),
    [
        (0, True, "restored this unit's services"),
        (SERVICES_NOT_READY_EXIT_CODE, False, "did not pass its readiness probe"),
        (7, False, "failed (exit 7)"),
    ],
)
def test_services_restore_maps_the_child_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    returncode: int,
    expected: bool,
    message: str,
) -> None:
    monkeypatch.setattr(command, "_repo_root", lambda: _fake_repo(tmp_path))
    monkeypatch.setattr(
        command.subprocess,
        "run",
        lambda *_a, **_kw: type("_R", (), {"returncode": returncode})(),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert command._compensate_services_restore(frozenset()) is expected
    captured = capsys.readouterr()
    assert message in captured.out + captured.err


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("timeout", "did not finish within 600s"),
        ("oserror", "could not be launched"),
    ],
)
def test_services_restore_reports_a_child_that_never_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: str,
    message: str,
) -> None:
    monkeypatch.setattr(command, "_repo_root", lambda: _fake_repo(tmp_path))

    def _run(cmd: list[str], **kwargs: object) -> object:
        if error == "timeout":
            raise command.subprocess.TimeoutExpired(cmd, float(kwargs["timeout"]))  # pyright: ignore[reportArgumentType]
        raise OSError("launch refused")

    monkeypatch.setattr(command.subprocess, "run", _run)
    assert command._compensate_services_restore(frozenset()) is False
    assert message in capsys.readouterr().err
