"""base.paths — per-agent workspace layout."""

import os
from pathlib import Path

import pytest

from base import paths
from base.daemon import health
from base.daemon.endpoints import ServiceEndpoints


def test_daemon_pidfile_uses_run_directory_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)
    current = ServiceEndpoints.from_settings().of("agent_host").pidfile
    assert current == tmp_path / "run" / "agent_host.pid"
    (tmp_path / current.name).write_text(str(os.getpid()))

    assert health._recorded_pid(current) is None

    current.write_text(str(os.getpid()))
    assert health._recorded_pid(current) == os.getpid()


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


def test_workspace_dir_none_fails_fast(unit_home: Path) -> None:
    """A None agent_id must explode, not silently create a `workspaces/None`
    directory — callers that legitimately have no agent id (pre-bootstrap) are
    expected to branch to Path.home() themselves before calling this."""
    with pytest.raises(ValueError, match="workspace_dir"):
        paths.workspace_dir(None)  # pyright: ignore[reportArgumentType]
    assert not (unit_home / "workspaces" / "None").exists()


# ── prod_service_checkout_error (Task #966: 01:13 worktree accident) ──


def _home_with_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".ava"
    (home / "source").mkdir(parents=True)
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


def test_prod_checkout_guard_allows_the_homes_own_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home's own source checkout is the one allowed launch site."""
    home = _home_with_source(tmp_path, monkeypatch)
    assert paths.prod_service_checkout_error(home / "source") is None


def test_prod_checkout_guard_refuses_dev_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree (under the home or anywhere else) must never launch a home's
    services: deleting the worktree removes the floor under the running fleet."""
    home = _home_with_source(tmp_path, monkeypatch)
    for repo in (home / "worktrees" / "ava-2750-dev-wt", home / "source-wt", tmp_path / "Ava"):
        err = paths.prod_service_checkout_error(repo)
        assert err is not None, repo
        assert str(home / "source") in err


def test_prod_checkout_guard_allows_a_home_without_source_any_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home with no `source` of its own (a test or scratch home) runs whichever
    checkout's code launches it — the guard is about homes that have an owner."""
    home = tmp_path / ".ava-dev"
    home.mkdir()
    monkeypatch.setenv("AVA_HOME", str(home))
    assert paths.prod_service_checkout_error(tmp_path / "worktrees" / "dev-wt") is None


def test_file_anchored_roots_resolve_to_the_checkout_root() -> None:
    """Modules that locate the checkout from their own `__file__` count their
    package depth: the paths door and the Grafana dashboard supplier's builtin
    plugins directory all land on this checkout."""
    from base.telemetry.metrics import grafana_dashboard_supply

    checkout = Path(__file__).resolve().parents[2]
    assert (checkout / "base" / "__init__.py").is_file()
    assert paths.repo_root() == checkout
    assert checkout / "ava_builtins" / "plugins" == grafana_dashboard_supply._REPO_PLUGINS_DIR
