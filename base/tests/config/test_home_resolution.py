"""The home is `$MEIVA_HOME` when set, else `~/.meiva`, read every time it is asked.

`base.paths.home.resolve_meiva_home` is the one resolver; `base.paths.meiva_home()`
is the in-process door built on it. Nothing captures the home at import, there is no
pointer file and no in-process injection channel: a process that changes the variable
is followed by every later call.

The default-home cases point HOME at a temporary directory, so a bug here can never
read, create or write the operator's real `~/.meiva`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from base import config, paths
from base.config.base import unit_home
from base.host.env import bootstrap, dotenv_file
from base.paths.home import resolve_meiva_home

_REPO = Path(__file__).resolve().parents[3]
_CHILD = Path(__file__).with_name("_config_child.py")


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME under a temporary directory, with MEIVA_HOME unset."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MEIVA_HOME", raising=False)
    return home


def test_unset_resolves_to_dot_meiva_under_home(fake_home: Path) -> None:
    assert resolve_meiva_home() == fake_home / ".meiva"


def test_empty_counts_as_unset(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEIVA_HOME", "")
    assert resolve_meiva_home() == fake_home / ".meiva"


def test_explicit_value_is_used_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path / ".meiva-gateway"))
    assert resolve_meiva_home() == tmp_path / ".meiva-gateway"


def test_tilde_expands_against_home(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEIVA_HOME", "~/.meiva-x")
    assert resolve_meiva_home() == fake_home / ".meiva-x"


def test_a_relative_home_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative home would follow the working directory."""
    monkeypatch.setenv("MEIVA_HOME", "relative/home")
    with pytest.raises(ValueError, match="absolute"):
        resolve_meiva_home()


def test_meiva_home_door_creates_the_default_home_under_the_fake_home(fake_home: Path) -> None:
    assert not (fake_home / ".meiva").exists()
    assert paths.meiva_home() == fake_home / ".meiva"
    assert (fake_home / ".meiva").is_dir()


def test_a_fresh_interpreter_with_no_variable_resolves_to_dot_meiva(tmp_path: Path) -> None:
    """Production depends on no variable: a process that never saw one lands on
    `~/.meiva`. Run in a child so nothing the test session pinned can leak in."""
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "MEIVA_HOME"}
    env["HOME"] = str(fake_home)
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, str(_CHILD), "meiva_home"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(fake_home / ".meiva")
    assert (fake_home / ".meiva").is_dir()


# Every base consumer that once captured the home at import (or bound it through the
# Settings field) now derives its path when called. Each row maps a home to the path
# the consumer must yield for it. The consumers that live in other packages (the
# memory pool, the OCR binary, the permissions-helper build directory) carry the same
# test in their own package's tests.
_CONSUMERS: list[tuple[str, Callable[[], Path], str]] = [
    ("paths.meiva_home", paths.meiva_home, ""),
    ("paths.logs_dir", paths.logs_dir, "logs"),
    ("dotenv_file.env_file_path", dotenv_file.env_file_path, ".env"),
    ("bootstrap snapshot", bootstrap.snapshot_path, "run/bootstrap-snapshot.json"),
    ("config path-field default", unit_home, ""),
]


@pytest.mark.parametrize(
    ("name", "consumer", "relative"), _CONSUMERS, ids=[c[0] for c in _CONSUMERS]
)
def test_every_consumer_follows_the_variable_after_import(
    name: str,
    consumer: Callable[[], Path],
    relative: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modules are imported; the variable changes afterwards, twice; each call
    answers for the home named at that moment."""
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("MEIVA_HOME", str(first))
    assert consumer() == first / relative, name
    monkeypatch.setenv("MEIVA_HOME", str(second))
    assert consumer() == second / relative, name


def test_the_config_surface_carries_no_home_field() -> None:
    """The home is not configuration: a Settings field would be a second source
    captured at construction."""
    assert "meiva_home" not in config.field_names()
    assert "MEIVA_HOME" not in config.field_alias_map().values()
