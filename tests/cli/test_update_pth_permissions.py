"""Update flows coexist with an emergency read-only editable-install pointer."""

from __future__ import annotations

import contextlib
import stat
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _update_agent_runner as _runner
from cli.commands import _update_local as _local
from cli.commands import _update_recover as _recover
from cli.commands import _update_uv_sync as _native_sync
from cli.commands import start as _start
from cli.commands import update as _up


@pytest.fixture(autouse=True)
def _canonical_sync_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transport stubs independent of this developer machine's mirror."""
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple")
    for repo in (tmp_path, tmp_path / "source"):
        repo.mkdir(exist_ok=True)
        (repo / "uv.lock").write_text("version = 1\npackage = []\n")


def _uv_step(args: list[str]) -> str:
    if args[:2] == ["uv", "export"]:
        assert "--locked" in args and "--offline" in args
        return "export"
    assert args == _native_sync._PROD_SYNC_ARGS
    return "sync"


def _read_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _read_only_pth(repo: Path) -> Path:
    pth = repo / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(repo))
    pth.chmod(0o444)
    return pth


def _passing_import_gate(
    _repo: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    return ()


def test_gateway_update_makes_pth_writable_only_while_uv_sync_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Removing the write window must expose 0444 to uv or leave 0644 afterwards."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    modes_during_sync: list[int] = []

    def sync_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        _uv_step(args)
        modes_during_sync.append(_read_mode(pth))
        return SimpleNamespace(returncode=0)

    def checkout(_sha: str) -> str:
        return "from-sha"

    def ignore_installed(_sha: str) -> None:
        return None

    monkeypatch.setattr(_up, "git_checkout_sha", checkout)
    monkeypatch.setattr(_native_sync, "run_bounded", sync_run)
    monkeypatch.setattr(_native_sync, "editable_import_gate", _passing_import_gate)
    monkeypatch.setattr("shared.source_integrity.set_installed", ignore_installed)

    result = _local._checkout_and_sync(
        repo,
        "target-sha",
        ("from-sha", set(), None),
        frozenset(),
    )

    assert result is None
    assert modes_during_sync == [0o644, 0o644]
    assert _read_mode(pth) == 0o444


def test_start_source_integrity_sync_restores_read_only_pth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Start's direct auto-heal sync shares the same protected write window."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    modes_during_sync: list[int] = []
    installed: list[str] = []

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        assert args == ["git", "rev-parse", "HEAD"]
        return SimpleNamespace(returncode=0, stdout="target-sha\n")

    def sync_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        _uv_step(args)
        modes_during_sync.append(_read_mode(pth))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_start.subprocess, "run", run)
    monkeypatch.setattr(_native_sync, "run_bounded", sync_run)
    monkeypatch.setattr("shared.source_integrity.get", lambda: "installed-sha")
    monkeypatch.setattr("shared.source_integrity.set_installed", installed.append)

    assert _start._verify_source_integrity(repo) == 0

    assert modes_during_sync == [0o644, 0o644]
    assert _read_mode(pth) == 0o444
    assert installed == ["target-sha"]


def test_gateway_recovery_restores_pth_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recovery start must observe the original protection, even after sync succeeds."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    observed: list[tuple[str, int]] = []

    def sync_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        observed.append((_uv_step(args), _read_mode(pth)))
        return SimpleNamespace(returncode=0)

    def start_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        observed.append(("start", _read_mode(pth)))
        return SimpleNamespace(returncode=0)

    def rollback_schema(_keep: set[str], *, local_admin: bool = False) -> list[str]:
        return []

    def reset(_sha: str) -> None:
        return None

    monkeypatch.setattr(_recover, "rollback_schema_to", rollback_schema)
    monkeypatch.setattr(_recover, "git_reset_hard", reset)
    monkeypatch.setattr(_native_sync, "run_bounded", sync_run)
    monkeypatch.setattr(_recover.subprocess, "run", start_run)

    result = _recover._recover_gateway_local(
        repo,
        "from-sha",
        {"baseline"},
        preserve_sessions=frozenset(),
    )

    assert result == 0
    assert observed == [("export", 0o644), ("sync", 0o644), ("start", 0o444)]
    assert _read_mode(pth) == 0o444


def test_agent_runner_failed_sync_still_restores_read_only_pth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-zero uv exit must take the same finally path as a successful update."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    modes_during_sync: list[int] = []

    def sync_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        _uv_step(args)
        modes_during_sync.append(_read_mode(pth))
        return SimpleNamespace(returncode=int(args[1] == "sync"))

    def checkout(_sha: str) -> str:
        return "from-sha"

    monkeypatch.setattr(_runner, "_source_switch_window", contextlib.nullcontext)
    monkeypatch.setattr(_runner, "git_checkout_sha", checkout)
    monkeypatch.setattr(_native_sync, "run_bounded", sync_run)
    monkeypatch.setattr(_native_sync, "editable_import_gate", _passing_import_gate)

    result = _runner._run_agent_runner_self_update_inner(
        repo,
        target_sha="target-sha",
        mode="none",
    )

    assert result == 1
    assert modes_during_sync == [0o644, 0o644]
    assert _read_mode(pth) == 0o444


def test_native_update_sync_entry_restores_read_only_pth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Windows shell update chain needs a Python write-window seam around uv."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    modes_during_sync: list[int] = []
    monkeypatch.setattr(_native_sync, "_repo_root", lambda: repo)

    def sync_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        _uv_step(args)
        modes_during_sync.append(_read_mode(pth))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", sync_run)
    assert _native_sync._main() == 0

    assert modes_during_sync == [0o644, 0o644]
    assert _read_mode(pth) == 0o444
