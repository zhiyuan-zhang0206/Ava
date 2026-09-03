"""Prepared CLI never silently falls through to old source/gateway dispatch."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import main
from cli.prepared_update import prepare_operator_input, run_prepared_update
from shared.runtime_release import ReleaseRejectedError


@pytest.mark.parametrize(
    "tail",
    [
        ["--prepared", "/absent"],
        ["--local", "--prepared=/absent", "--force"],
        ["--local", "--prepared=/absent", "--restart-only"],
        ["--local", "--prepared=/absent", "--dry-run"],
        ["--local", "--prepared=/absent", "--mode", "force"],
    ],
)
def test_invalid_combination_refuses_before_plan_or_old_dispatch(
    monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    def forbidden(_path: Path) -> None:
        raise AssertionError("invalid flags must not read a plan")

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", forbidden)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", *tail]) == 2


def test_prepared_enters_handler_before_checkout_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def prepared(args: argparse.Namespace) -> int:
        seen.append(args.prepared)
        return 17

    def forbidden(_verb: str) -> None:
        raise AssertionError("wheel entry must not ask an absent checkout for its home")

    monkeypatch.setattr("cli.prepared_update.run_prepared_update", prepared)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", "--local", "--prepared", "/private/plan"]) == 17
    assert seen == ["/private/plan"]


def test_source_cannot_impersonate_retained_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli.prepared_update.WHEEL_RUNTIME", False)
    with pytest.raises(ReleaseRejectedError, match="retained POSIX"):
        prepare_operator_input(Path("/must-not-be-read"))


def test_unknown_prepared_flag_is_not_silently_forwarded() -> None:
    with pytest.raises(SystemExit) as error:
        main.main(["cluster", "update", "--local", "--prepared", "/x", "--permit-ready"])
    assert error.value.code == 2


def test_preparation_error_returns_refusal_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_path: Path) -> None:
        raise ReleaseRejectedError("unknown LKG")

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", refuse)
    args = main._build_parser().parse_args(["cluster", "update", "--local", "--prepared", "/x"])
    assert run_prepared_update(args) == 2


def test_actual_parser_refuses_without_importing_settings_or_commands(tmp_path: Path) -> None:
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'shared.config' or fullname == 'cli.commands':
            raise AssertionError('forbidden early import: ' + fullname)
sys.meta_path.insert(0, Deny())
from cli.main import main
assert main(['cluster', 'update', '--prepared', '/absent']) == 2
assert main(['cluster', 'update', '--local', '--prepared', '/absent']) == 2
"""
    environment = {**os.environ, "HOME": str(tmp_path), "AVA_HOME": str(tmp_path / "unit")}
    result = subprocess.run(  # noqa: S603 — fixed current interpreter and literal import-guard program.
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
