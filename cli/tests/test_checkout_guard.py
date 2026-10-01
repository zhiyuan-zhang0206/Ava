"""A home that carries its own `<home>/source` checkout is changed only by that checkout.

The rule (`base.host.env.dotenv_boot.home_checkout_error`) is shared by the CLI's
pre-Settings gate (`cli.preflight.require_own_checkout`), the identity
boundaries of `ava init` and `ava start` (`cli.init_intent`, `cli.start_intent`) and the service-launch guard
(`base.paths.prod_service_checkout_error`). A home with no `source` of its own (a
test or scratch home) accepts any checkout. HOME points at a temporary directory
wherever the default home is involved, so none of this can touch the operator's
real `~/.ava`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from base.host.env.dotenv_boot import home_checkout_error
from cli import fleet_update, init_intent, preflight, start_intent


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
    """The comparison resolves both sides: on macOS /tmp is itself a symlink."""
    home = _home_with_source(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.symlink_to(home / "source")
    assert home_checkout_error(alias) is None


def test_the_default_home_is_guarded_when_it_has_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With AVA_HOME unset the home is `~/.ava` (here under a fake HOME): the
    production shape, where every other checkout is refused."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AVA_HOME", raising=False)
    (tmp_path / ".ava" / "source").mkdir(parents=True)
    assert home_checkout_error(tmp_path / ".ava" / "source") is None
    assert home_checkout_error(tmp_path / "Ava") is not None


# ── the CLI gate: a read-only list, everything else refused ──


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["maintenance", "status"],
        ["cluster", "status"],
        ["agents", "ls"],
        ["config", "get", "AVA_TIMEZONE"],
        ["mcp", "ls"],
    ],
)
def test_gate_passes_the_read_only_verbs(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home_with_source(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, tmp_path / "dev") is None


@pytest.mark.parametrize(
    "argv",
    [
        ["start"],
        ["stop"],
        ["pause"],
        ["restart"],
        ["converge"],
        ["maintenance", "start"],
        ["maintenance", "stop"],
        ["cluster", "destroy"],
        ["cluster", "db-authority", "issue-unit"],
        ["config", "set", "A=B"],
        ["config", "unset", "A"],
        ["boot"],
        ["some-verb-the-list-never-heard-of"],
    ],
)
def test_gate_refuses_every_other_verb_including_unknown_ones(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed: a verb that is not on the read-only list is refused, so a
    verb added later is gated until someone decides it changes nothing."""
    _home_with_source(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, tmp_path / "dev") == 1
    assert " ".join(argv[:2]) in capsys.readouterr().err


def test_gate_passes_everything_for_the_homes_own_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(["stop"], home / "source") is None


# ── init and start ──


def test_start_refuses_a_foreign_checkout_before_reading_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    monkeypatch.setattr(start_intent, "_checkout", lambda: tmp_path / "dev")

    def _never_reached(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a refused start must not reach the home's admission")

    monkeypatch.setattr(start_intent, "require_initialized", _never_reached)
    args = argparse.Namespace()

    assert start_intent.run_start(args) == 1

    assert "source checkout" in capsys.readouterr().err
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"


def test_init_refuses_a_foreign_checkout_before_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _home_with_source(tmp_path, monkeypatch)
    monkeypatch.setattr(start_intent, "_checkout", lambda: tmp_path / "dev")

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
    monkeypatch.setattr(start_intent, "_checkout", lambda: tmp_path / "dev")
    assert start_intent._home() == (tmp_path / "home").resolve()


# ── the fleet updater already drives each host's own checkout ──


def test_the_fleet_updater_runs_the_homes_own_cli_on_every_host() -> None:
    """`cli.fleet_update` stops and starts each unit through `$H/source/.venv/bin/ava`
    with `AVA_HOME="$H"`: the home's own checkout, so the gate never blocks it."""
    assert 'S="$H/source"' in fleet_update._HOME
    assert fleet_update._AVA == 'AVA_HOME="$H" "$S/.venv/bin/ava"'
