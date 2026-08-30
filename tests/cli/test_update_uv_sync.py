"""Production-scoped ``uv sync``: dev-group exclusion, frozen lock, bounded runtime, terminal outcomes.

The 2026-08-30 rollout stalled on the Windows agent-runner, whose updater ran a
bare ``uv sync`` and spent 449 s downloading the dev-only pyright 1.1.411 wheel
(RAM at 97.3%, uv log stuck at "Downloading pyright (5.9MiB)"). These tests pin
the properties the fix ships: production sync excludes dev-only deps and never
re-resolves a drifted lockfile (``--frozen``), the Windows cache is pinned to a
stable path, every sync is bounded and leaves progress evidence, and every
failure mode (including a hang) lands as a diagnosable terminal outcome instead
of silence. The dependency-group boundary tests at the bottom lock the split
itself: dev-only tools must stay in the dev group and out of production imports.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _update_uv_sync as _native_sync
from shared.deploy_timing import UV_SYNC_TIMEOUT_S

_EXPECTED_ARGS = ["uv", "sync", "--frozen", "--no-dev", "--inexact", "--verbose"]


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
    by an update), fail fast on a drifted lockfile (`--frozen` — the committed
    uv.lock is the single source of runtime pins), keep the already-installed
    dev packages (`--inexact`), carry a progress heartbeat (`--verbose`), and
    run under `run_bounded` with the deploy-family ceiling. Only `--frozen` is
    allowed: `--locked` would additionally require the lock to be in sync with
    the pyproject *before* resolving, a stricter guarantee than the rollout
    flow needs."""
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
    # Lockfile semantics are frozen, not re-resolved: `--frozen` in, `--locked`
    # out (a lock change lands through the deliberate `uv lock` + rollout flow).
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--frozen" in argv
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


# ─── Windows uv cache pinning ────────────────────────────────────────────────


def test_win_sync_pins_uv_cache_to_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On Windows the production sync must pin UV_CACHE_DIR to a stable path
    under %LOCALAPPDATA% — a lockfile introducing a new wheel then downloads it
    once, not on every rollout."""
    monkeypatch.setattr(_native_sync, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    seen: dict[str, object] = {}

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", _run)

    assert _native_sync.run_uv_sync(tmp_path).returncode == 0
    assert os.environ["UV_CACHE_DIR"] == str(tmp_path / "localappdata" / "ava" / "uv-cache")


def test_win_sync_falls_back_to_ava_home_without_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Task-Scheduler updater that runs without LOCALAPPDATA still gets a
    stable cache: the ava home directory."""
    monkeypatch.setattr(_native_sync, "IS_WINDOWS", True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(_native_sync, "ava_home", lambda: tmp_path / "ava-home")

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", _run)

    assert _native_sync.run_uv_sync(tmp_path).returncode == 0
    assert os.environ["UV_CACHE_DIR"] == str(tmp_path / "ava-home" / "uv-cache")


def test_posix_sync_keeps_uv_default_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """POSIX hosts keep uv's default cache (~/.cache/uv, already stable):
    pinning a different path would discard the warm cache every rollout."""
    monkeypatch.setattr(_native_sync, "IS_WINDOWS", False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    seen: dict[str, object] = {}

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", _run)

    assert _native_sync.run_uv_sync(tmp_path).returncode == 0
    assert "UV_CACHE_DIR" not in os.environ


# ─── dependency-group boundary ───────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEV_ONLY_TOOLS = {"pyright", "pytest", "ruff"}


def _dep_names(specs: list[str]) -> set[str]:
    """Top-level package names from PEP 508 specs (`name`, `name[extra]>=x`)."""
    names: set[str] = set()
    for spec in specs:
        name = spec.split("[", 1)[0].split(">=", 1)[0].split("==", 1)[0]
        names.add(name.strip().lower())
    return names


def test_dev_group_holds_the_dev_only_tools() -> None:
    """The group split is the whole fix: pyright & friends live ONLY in the dev
    group, never in [project].dependencies. A move back to dependencies would
    make production sync download dev wheels again on every lock change."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    dev_names = _dep_names(pyproject["dependency-groups"]["dev"])
    prod_names = _dep_names(pyproject["project"]["dependencies"])
    missing = _DEV_ONLY_TOOLS - dev_names
    leaked = _DEV_ONLY_TOOLS & prod_names
    assert not missing, f"dev group lost a dev-only tool: {missing}"
    assert not leaked, f"dev-only tool leaked into prod deps: {leaked}"


def test_prod_source_never_imports_dev_only_modules() -> None:
    """Production code (everything outside tests/) must not import dev-only
    tooling — if it did, --no-dev production sync would leave the runtime
    missing an import. Scans the import surface statically."""
    dev_only_modules = {
        "pytest",
        "pyright",
        "ruff",
        "import_linter",
        "mutmut",
        "pre_commit",
        "httpx2",
        "pytest_asyncio",
        "pytest_playwright",
        "pytest_cov",
        "pytest_xdist",
        "pytest_split",
    }
    prod_dirs = ["ava", "cli", "agent", "gateway", "services", "shared", "ops"]
    hits: list[str] = []
    for d in prod_dirs:
        base = _REPO_ROOT / d
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in dev_only_modules:
                            hits.append(f"{py.relative_to(_REPO_ROOT)}: import {alias.name}")
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] in dev_only_modules
                ):
                    hits.append(f"{py.relative_to(_REPO_ROOT)}: from {node.module}")
    assert not hits, "production code imports dev-only tooling:\n" + "\n".join(hits)
