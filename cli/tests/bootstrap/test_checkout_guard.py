from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from base.paths import ava_home
from base.paths.home import home_checkout_error
from cli import fleet_update, init_intent, preflight, start_intent
from cli.database import OperatorDatabaseFactory, operator_event_pipeline
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


def _home_with_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".ava"
    (home / "source").mkdir(parents=True)
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


def test_a_home_without_source_accepts_any_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    assert home_checkout_error(tmp_path / "some-worktree") is None


def test_the_homes_own_source_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    assert home_checkout_error(home / "source") is None


def test_another_checkout_is_refused_with_both_paths_and_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    error = home_checkout_error(tmp_path / "worktrees" / "dev")
    assert error is not None
    assert str(tmp_path / "worktrees" / "dev") in error
    assert str(home) in error
    assert str(home / "source" / ".venv" / "bin" / "ava") in error
    assert "set AVA_HOME" in error


def test_a_symlinked_spelling_of_the_same_checkout_is_the_same_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.symlink_to(home / "source")
    assert home_checkout_error(alias) is None


def test_the_default_home_is_guarded_when_it_has_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AVA_HOME", raising=False)
    (tmp_path / ".ava" / "source").mkdir(parents=True)
    assert home_checkout_error(tmp_path / ".ava" / "source") is None
    assert home_checkout_error(tmp_path / "Ava") is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["status", "--json"],
        ["cluster", "status"],
        ["agents", "ls"],
        ["config", "get", "AVA_TIMEZONE"],
        ["mcp", "ls"],
        ["start"],
        ["stop"],
        ["restart"],
        ["converge"],
        ["cluster", "destroy"],
        ["cluster", "db-authority", "issue-unit"],
        ["config", "set", "A=B"],
        ["config", "unset", "A"],
        ["some-verb-nobody-has-heard-of"],
        ["start", "--help"],
        ["status", "-h"],
        ["--help", "start"],
        ["-h", "--help"],
        ["config", "unset", "--", "--help"],
    ],
)
def test_gate_refuses_every_command_from_a_foreign_checkout(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, tmp_path / "dev") == 1
    err = capsys.readouterr().err
    assert str(home / "source" / ".venv" / "bin" / "ava") in err, "run the home's own CLI"
    assert "set AVA_HOME" in err, "or name a home of your own"


def test_gate_does_not_echo_the_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _home_with_source(tmp_path, monkeypatch)
    argv = ["config", "set", "DEEPSEEK_API_KEY=sk-not-for-the-terminal"]
    assert preflight.require_own_checkout(argv, tmp_path / "dev") == 1
    assert "sk-not-for-the-terminal" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
def test_gate_passes_bare_ava_and_a_lone_help_flag(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home_with_source(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, tmp_path / "dev") is None


@pytest.mark.parametrize(
    "argv", [["status"], ["stop"], ["start", "--help"], ["config", "set", "A=B"]]
)
def test_gate_passes_everything_for_the_homes_own_checkout(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, home / "source") is None


@pytest.mark.parametrize(
    "argv", [["status"], ["stop"], ["start", "--help"], ["config", "set", "A=B"]]
)
def test_gate_passes_everything_for_a_home_without_a_source(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    assert preflight.require_own_checkout(argv, tmp_path / "dev") is None


def test_start_refuses_a_foreign_checkout_before_reading_the_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> None:
    producer = operator_event_pipeline(
        operator_database, machine=command_runtime.machine, home=ava_home()
    )
    home = _home_with_source(tmp_path, monkeypatch)
    monkeypatch.setattr(start_intent, "checkout", lambda: tmp_path / "dev")

    def _never_reached(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a refused start must not reach the home's admission")

    monkeypatch.setattr(start_intent, "require_initialized", _never_reached)
    args = argparse.Namespace(runtime=command_runtime, boot=False)

    assert (
        start_intent.run_start(
            args, retained_children=[], database_factory=operator_database, producer=producer
        )
        == 1
    )

    assert "source checkout" in capsys.readouterr().err
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"


def test_init_refuses_a_foreign_checkout_before_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    monkeypatch.setattr(start_intent, "checkout", lambda: tmp_path / "dev")

    def _never_reached(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a refused init must not reach the identity phase")

    monkeypatch.setattr(init_intent, "initialize_home", _never_reached)

    assert init_intent.run_init(argparse.Namespace()) == 1

    assert "source checkout" in capsys.readouterr().err
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"


def test_a_test_home_is_taken_from_any_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(start_intent, "checkout", lambda: tmp_path / "dev")
    assert start_intent._home() == (tmp_path / "home").resolve()


def test_the_fleet_updater_runs_the_homes_own_cli_on_every_host() -> None:
    assert 'S="$H/source"' in fleet_update.HOME
    assert fleet_update.AVA == 'AVA_HOME="$H" "$S/.venv/bin/ava"'
