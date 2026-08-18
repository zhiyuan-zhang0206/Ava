"""shared.paths — per-agent workspace layout."""

from pathlib import Path

import pytest

from shared import paths


def test_workspace_dir_creates_per_agent_dir(unit_home: Path) -> None:
    p = paths.workspace_dir(42)
    assert p == unit_home / "workspaces" / "42"
    assert p.is_dir()


def test_workspace_dir_idempotent_and_per_agent(unit_home: Path) -> None:
    first = paths.workspace_dir(42)
    assert paths.workspace_dir(42) == first
    other = paths.workspace_dir(7)
    assert other == unit_home / "workspaces" / "7"
    assert other != first


# ── prod_service_checkout_error (Task #966: 01:13 worktree accident) ──


def test_prod_checkout_guard_allows_anchored_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod home's own anchored checkout is the one allowed launch site."""
    monkeypatch.setattr(paths, "ava_home", lambda: Path.home() / ".ava")
    assert paths.prod_service_checkout_error(Path.home() / ".ava" / "source") is None


def test_prod_checkout_guard_refuses_dev_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worktree under ~/.ava/worktrees (or anywhere else) must never launch
    prod services: it resolves to the prod home as an unanchored checkout, and
    deleting the worktree removes the floor under the running fleet."""
    monkeypatch.setattr(paths, "ava_home", lambda: Path.home() / ".ava")
    for repo in (
        Path.home() / ".ava" / "worktrees" / "ava-2750-dev-wt",
        Path.home() / ".ava" / "source-wt",
        Path.home() / "Ava",
    ):
        err = paths.prod_service_checkout_error(repo)
        assert err is not None, repo
        assert "01:13 worktree accident" in err
        assert str(Path.home() / ".ava" / "source") in err


def test_prod_checkout_guard_allows_dev_home_any_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dev unit (home != ~/.ava) runs its own checkout's code by design —
    the guard is only about the prod home."""
    dev_home = tmp_path / ".ava-dev"
    dev_home.mkdir()
    monkeypatch.setattr(paths, "ava_home", lambda: Path(str(dev_home)))
    assert paths.prod_service_checkout_error(Path.home() / ".ava" / "worktrees" / "dev-wt") is None
