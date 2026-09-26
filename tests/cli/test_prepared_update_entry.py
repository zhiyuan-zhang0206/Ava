"""The prepared release request is the only update CLI entry."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import main


@pytest.mark.parametrize(
    "tail",
    [
        ["--local", "--prepared", "/absent"],
        ["--local", "--prepared=/absent", "--force"],
        ["--local", "--prepared=/absent", "--restart-only"],
        ["--local", "--prepared=/absent", "--dry-run"],
        ["--local", "--prepared=/absent", "--mode", "force"],
    ],
)
def test_invalid_combination_refuses_before_plan_or_old_dispatch(
    monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    def forbidden(_path: Path) -> int:
        raise AssertionError("invalid flags must not read a plan")

    monkeypatch.setattr("cli.release_transition.submit.run", forbidden)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    with pytest.raises(SystemExit) as exited:
        main.main(["cluster", "update", *tail])
    assert exited.value.code == 2


def test_prepared_enters_handler_before_checkout_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def prepared(path: Path) -> int:
        seen.append(str(path))
        return 17

    def forbidden(_verb: str) -> None:
        raise AssertionError("wheel entry must not ask an absent checkout for its home")

    monkeypatch.setattr("cli.release_transition.submit.run", prepared)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", "--prepared", "/private/plan"]) == 17
    assert seen == ["/private/plan"]


def test_unknown_prepared_flag_is_not_silently_forwarded() -> None:
    with pytest.raises(SystemExit) as error:
        main.main(["cluster", "update", "--prepared", "/x", "--permit-ready"])
    assert error.value.code == 2


def test_actual_parser_refuses_without_importing_settings_or_commands(tmp_path: Path) -> None:
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'shared.config', 'cli.commands'}:
            raise AssertionError('forbidden early import: ' + fullname)
sys.meta_path.insert(0, Deny())
from cli.main import main
assert main(['cluster', 'update', '--prepared', '/absent']) == 2
assert main(['cluster', 'update', '--prepar', '/absent']) == 2
try:
    main(['cluster', 'update', '--prepared', '/absent', '--local'])
except SystemExit as error:
    assert error.code == 2
else:
    raise AssertionError('removed updater flags must refuse at parsing')
for verb in ('restart', 'rollback', 'cancel', 'recover-pending'):
    try:
        main(['cluster', verb])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError('retired release command still exists: ' + verb)
"""
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "AVA_HOME": str(tmp_path / "unit"),
        "AVA_CLI_LOG_NAME": "prepared-import-guard",
    }
    result = subprocess.run(  # noqa: S603 — fixed interpreter and literal guard program.
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
