"""`ava start` refuses to bind the prod home to a dev checkout (Task #966).

The 01:13 worktree accident: a worktree with no `.ava_home` pointer resolves as
an unanchored checkout to the prod home, so `ava start` from it launched every
prod daemon off that worktree's code — and routine worktree cleanup then
removed the floor under the running fleet. The prod home may only be launched
from its own anchored checkout (`~/.ava/source`).

These cover the start-side of the guard (the respawn side lives in
`tests/shared/test_service_respawn.py`; the predicate itself in
`tests/shared/test_paths.py`).
"""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import pytest

from cli.commands import start as _start
from shared import paths


def _run_start(monkeypatch: pytest.MonkeyPatch, repo: Path, home: Path) -> tuple[int, str]:
    monkeypatch.setattr(paths, "ava_home", lambda: Path(str(home)))
    monkeypatch.setattr(_start, "_repo_root", lambda: Path(str(repo)))
    buf = StringIO()
    with redirect_stderr(buf):
        rc = _start._cmd_start_body()
    return rc, buf.getvalue()


def test_start_refuses_prod_home_from_a_dev_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, err = _run_start(
        monkeypatch,
        repo=Path.home() / ".ava" / "worktrees" / "ava-2750-dev-wt",
        home=Path.home() / ".ava",
    )
    assert rc == 1
    assert "01:13 worktree accident" in err
    assert str(Path.home() / ".ava" / "source") in err


def test_start_refuses_prod_home_from_dev_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev clone ~/Ava is not the prod home's anchored checkout either —
    launching prod services from it is the same hazard class."""
    rc, err = _run_start(
        monkeypatch,
        repo=Path.home() / "Ava",
        home=Path.home() / ".ava",
    )
    assert rc == 1
    assert "01:13 worktree accident" in err


def test_start_does_not_refuse_dev_home_from_a_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dev unit's own home anchored to a worktree checkout passes the guard —
    it runs its own code by design. (The start proceeds into the source-
    integrity step, which the test short-circuits; the point is the checkout
    guard did NOT fire.)"""
    dev_home = tmp_path / ".ava-dev"
    dev_home.mkdir()
    repo = Path.home() / ".ava" / "worktrees" / "ava-2750-dev-wt"
    monkeypatch.setattr(paths, "ava_home", lambda: Path(str(dev_home)))
    monkeypatch.setattr(_start, "_repo_root", lambda: Path(str(repo)))
    # abort at the next guard (source integrity) so nothing heavier runs
    monkeypatch.setattr(_start, "_verify_source_integrity", lambda _repo: 1)  # pyright: ignore[reportUnknownArgumentType]
    buf = StringIO()
    with redirect_stderr(buf):
        rc = _start._cmd_start_body()
    assert rc == 1
    assert "01:13 worktree accident" not in buf.getvalue()
