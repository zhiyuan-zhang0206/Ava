"""Production-scoped ``uv sync``: dev-group exclusion, bounded runtime, terminal outcomes.

The 2026-08-30 rollout stalled on the Windows agent-runner, whose updater ran a
bare ``uv sync`` and spent 449 s downloading the dev-only pyright 1.1.411 wheel
(RAM at 97.3%, uv log stuck at "Downloading pyright (5.9MiB)"). These tests pin
the three properties the fix ships: production sync excludes dev-only deps,
every sync is bounded and leaves progress evidence, and every failure mode
(including a hang) lands as a diagnosable terminal outcome instead of silence.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _update_uv_sync as _native_sync
from shared.deploy_timing import UV_SYNC_TIMEOUT_S

_EXPECTED_ARGS = ["uv", "sync", "--no-dev", "--inexact", "--verbose"]


def _read_only_pth(repo: Path) -> Path:
    pth = repo / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(repo))
    pth.chmod(0o444)
    return pth


def _read_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prod_sync_argv_excludes_dev_group_and_carries_the_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sync argv must exclude the dev group (no dev wheel is ever downloaded
    by an update), keep the already-installed dev packages (`--inexact`), carry a
    progress heartbeat (`--verbose`), and run under `run_bounded` with the
    deploy-family ceiling — while frozen/lock semantics stay untouched (no
    `--frozen` / `--locked` flag introduced)."""
    repo = tmp_path / "source"
    seen: dict[str, object] = {}

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        seen["kwargs"] = _kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", _run)

    result = _native_sync.run_uv_sync(repo)

    assert result.returncode == 0
    assert seen["argv"] == _EXPECTED_ARGS
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == repo
    assert kwargs["timeout"] == UV_SYNC_TIMEOUT_S
    assert kwargs["capture_output"] is False
    # The fix must not change lockfile / reproducible semantics.
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--frozen" not in argv
    assert "--locked" not in argv


def test_timeout_becomes_a_failed_result_with_a_diagnosable_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sync that stops making progress must not hang the updater: the bound
    fires, the reason is printed, and the caller's `returncode != 0` handling
    converts it into the terminal updater outcome — no exception, no silence."""
    repo = tmp_path / "source"

    def _hang(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=UV_SYNC_TIMEOUT_S)

    monkeypatch.setattr(_native_sync, "run_bounded", _hang)

    result = _native_sync.run_uv_sync(repo)

    assert result.returncode == 124
    err = capsys.readouterr().err
    assert "uv sync timed out after 600s" in err
    assert "last package uv was downloading" in err


def test_timeout_still_restores_the_read_only_pth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The editable-pointer write window must close on the timeout path too —
    a killed sync leaves the guard exactly as a finished one does."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    modes: list[int] = []

    def _hang(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        modes.append(_read_mode(pth))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=UV_SYNC_TIMEOUT_S)

    monkeypatch.setattr(_native_sync, "run_bounded", _hang)

    assert _native_sync.run_uv_sync(repo).returncode == 124
    assert modes == [0o644]
    assert _read_mode(pth) == 0o444


def test_missing_uv_is_a_failed_result_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sync that cannot even start (uv absent from PATH) is reported the same
    way as any other failure — a readable stderr line and a non-zero rc."""
    repo = tmp_path / "source"

    def _missing(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("no such executable: uv")

    monkeypatch.setattr(_native_sync, "run_bounded", _missing)

    result = _native_sync.run_uv_sync(repo)

    assert result.returncode == 127
    err = capsys.readouterr().err
    assert "uv sync could not start" in err
    assert "no such executable: uv" in err


def test_nonzero_exit_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plain uv failure keeps its own returncode — the caller's existing
    non-zero handling stays the single failure path."""
    repo = tmp_path / "source"

    def _fail(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(_native_sync, "run_bounded", _fail)

    assert _native_sync.run_uv_sync(repo).returncode == 3
