"""Production-scoped ``uv sync``: dev-group exclusion, locked lockfile, bounded runtime, terminal outcomes.

The 2026-08-30 rollout stalled on the Windows agent-runner, whose updater ran a
bare ``uv sync`` and spent 449 s downloading the dev-only pyright 1.1.411 wheel
(RAM at 97.3%, uv log stuck at "Downloading pyright (5.9MiB)"). These tests pin
the properties the fix ships: production sync excludes dev-only deps and
asserts lockfile freshness (``--locked`` — a drifted lock fails loudly instead
of silently installing the stale environment), the Windows cache is pinned to a
stable path, every sync is bounded and leaves progress evidence, and every
failure mode (including a hang) lands as a diagnosable terminal outcome instead
of silence. The dependency-group boundary tests at the bottom lock the split
itself: dev-only tools must stay in the dev group and out of production imports.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _update_uv_sync as _native_sync
from shared.deploy_timing import UV_SYNC_TIMEOUT_S


@pytest.fixture(autouse=True)
def _canonical_sync_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transport stubs independent of this developer machine's mirror."""
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple")
    for repo in (tmp_path, tmp_path / "source"):
        repo.mkdir(exist_ok=True)
        (repo / "uv.lock").write_text("version = 1\npackage = []\n")


def _expected_args(repo: Path) -> list[str]:
    """Mirror the implementation: the --python pin rides only when the venv
    interpreter already exists (a real host's converged venv). A fresh staging
    repo has no venv, so no pin — uv creates one."""
    python_name = "python.exe" if _native_sync.IS_WINDOWS else "python"
    interpreter = repo / ".venv" / _native_sync.get_backend().venv_bin_dir_name() / python_name
    argv = [*_native_sync._PROD_SYNC_ARGS]
    if interpreter.exists():
        argv += ["--python", str(interpreter)]
    return argv


def _fake_venv_python(repo: Path) -> Path:
    """Create the venv-interpreter marker a converged host would have."""
    python_name = "python.exe" if _native_sync.IS_WINDOWS else "python"
    interpreter = repo / ".venv" / _native_sync.get_backend().venv_bin_dir_name() / python_name
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.touch()
    return interpreter


def _read_only_pth(repo: Path) -> Path:
    pth = repo / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(repo))
    pth.chmod(0o444)
    return pth


def _editable_direct_url(repo: Path) -> Path:
    """Create the companion record uv writes for Ava's editable install."""

    record = (
        repo
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    record.parent.mkdir(parents=True)
    record.write_text('{"url":"file:///source","dir_info":{"editable":true}}')
    return record


def _read_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _passing_import_gate(
    _repo: Path,
    *,
    allowed_roots: Iterable[Path] = (),
) -> tuple[str, ...]:
    return ()


def test_prod_sync_argv_excludes_dev_group_and_carries_the_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sync argv must exclude the dev group (no dev wheel is ever downloaded
    by an update), assert lockfile freshness (`--locked` — the committed
    uv.lock is the single source of runtime pins, and a drift must fail loudly
    rather than silently install the stale environment), keep the
    already-installed dev packages (`--inexact`), carry a progress heartbeat
    (`--verbose`), and run under `run_bounded` with the deploy-family ceiling.
    Only `--locked` is allowed: `--frozen` merely skips lockfile *updates*
    (uv's default) and would not catch a drift."""
    repo = tmp_path / "source"
    _fake_venv_python(repo)  # a converged host's venv: the pin branch
    seen: dict[str, object] = {}
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "foreign-venv"))

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        seen["kwargs"] = _kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", _run)

    result = _native_sync.run_uv_sync(repo)

    assert result.returncode == 0
    assert seen["argv"] == _expected_args(repo)
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == repo
    assert 0 < kwargs["timeout"] <= UV_SYNC_TIMEOUT_S
    assert kwargs["capture_output"] is False
    assert "VIRTUAL_ENV" not in kwargs["env"]
    # Lockfile semantics are asserted, never silently re-resolved or ignored:
    # `--locked` in, `--frozen` out (a lock change lands through the deliberate
    # `uv lock` + rollout flow).
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--locked" in argv
    assert "--frozen" not in argv


def test_prod_sync_reinstalls_a_requested_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recovery caller can force uv to reinstall the missing Ava launcher."""

    repo = tmp_path / "source"
    argv_calls: list[list[str]] = []

    def run_uv(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        argv_calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", run_uv)

    assert _native_sync.run_uv_sync(repo, reinstall_package="ava").returncode == 0
    assert argv_calls == [
        [*_expected_args(repo), "--reinstall-package", "ava"],
    ]


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


def test_verified_sync_rejects_a_fake_successful_half_uninstall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A zero uv exit is not accepted until the venv import gate passes."""

    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    original = subprocess.CompletedProcess(["uv", "sync"], returncode=0, stdout=b"ok", stderr=None)

    def fake_sync(
        _repo: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
        reinstall_package: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        pth.unlink()
        return original

    monkeypatch.setattr(_native_sync, "run_uv_sync", fake_sync)

    def failing_import_gate(
        _repo: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ("editable import gate failed (pointer missing)",)

    monkeypatch.setattr(_native_sync, "editable_import_gate", failing_import_gate)

    result = _native_sync.run_uv_sync_verified(repo)

    assert result.returncode == 126
    assert result.stderr is not None
    assert b"pointer missing" in result.stderr


def test_verified_sync_preserves_a_healthy_sync_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A passing import proof preserves the successful sync result unchanged."""

    original = subprocess.CompletedProcess(["uv", "sync"], returncode=0, stdout=b"ok", stderr=None)

    def fake_sync(
        _repo: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
        reinstall_package: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return original

    monkeypatch.setattr(_native_sync, "run_uv_sync", fake_sync)
    monkeypatch.setattr(_native_sync, "editable_import_gate", _passing_import_gate)

    assert _native_sync.run_uv_sync_verified(tmp_path) is original


def test_preflight_success_starts_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A successful write, rename, and unlink probe permits the real uv path."""
    repo = tmp_path / "source"
    site_packages = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    calls: list[list[str]] = []

    def run_uv(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", run_uv)

    assert _native_sync.run_uv_sync(repo).returncode == 0
    assert calls == [_expected_args(repo)]
    assert list(site_packages.iterdir()) == []


def test_preflight_write_failure_stops_before_uv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A protected pointer boundary fails before uv can partially uninstall it."""
    repo = tmp_path / "source"
    site_packages = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    original_write_text = Path.write_text
    calls: list[list[str]] = []

    def deny_probe_write(
        path: Path,
        text: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.parent == site_packages and path.name.startswith(".ava-write-probe-"):
            raise PermissionError(13, "Permission denied", str(path))
        return original_write_text(
            path,
            text,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    def should_not_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(Path, "write_text", deny_probe_write)
    monkeypatch.setattr(_native_sync, "run_bounded", should_not_run)

    result = _native_sync.run_uv_sync(repo)

    assert result.returncode == 126
    assert calls == []
    assert list(site_packages.iterdir()) == []
    err = capsys.readouterr().err
    assert str(site_packages) in err
    assert "write operation" in err
    assert "Permission denied" in err


def test_failed_sync_restores_removed_editable_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero sync cannot leave either editable record half-uninstalled."""
    repo = tmp_path / "source"
    pth = _read_only_pth(repo)
    direct_url = _editable_direct_url(repo)
    before = {pth: pth.read_bytes(), direct_url: direct_url.read_bytes()}

    def remove_records(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        pth.unlink()
        direct_url.unlink()
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(_native_sync, "run_bounded", remove_records)

    assert _native_sync.run_uv_sync(repo).returncode == 3
    assert {path: path.read_bytes() for path in before} == before
    assert _read_mode(pth) == 0o444
    err = capsys.readouterr().err
    assert f"restored editable install record {pth}" in err
    assert f"restored editable install record {direct_url}" in err


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


def test_win_sync_respects_an_explicit_uv_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """setdefault, not assignment: an operator-pinned UV_CACHE_DIR wins over
    the built-in pin — replacing setdefault with direct assignment must fail
    this test (mutation-verified in adversarial QA)."""
    monkeypatch.setattr(_native_sync, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    explicit = str(tmp_path / "explicit-cache")
    monkeypatch.setenv("UV_CACHE_DIR", explicit)

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_native_sync, "run_bounded", _run)

    assert _native_sync.run_uv_sync(tmp_path).returncode == 0
    assert os.environ["UV_CACHE_DIR"] == explicit


# ─── real uv integration: --locked fails on a drifted lockfile ───────────────


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_locked_fails_on_a_drifted_lockfile_with_real_uv(tmp_path: Path) -> None:
    """End-to-end on the pinned toolchain: `uv sync --locked` must exit non-zero
    with an actionable message when pyproject.toml drifts from uv.lock — the
    exact silent-surprise class this seam exists to kill. `--frozen` alone
    exits 0 and installs the stale environment on uv 0.10.2 (verified)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    base = (
        '[project]\nname = "drift-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n'
    )
    (proj / "pyproject.toml").write_text(base)
    subprocess.run(["uv", "lock"], cwd=proj, check=True, capture_output=True, timeout=120)
    # Drift: a dependency is declared without re-locking.
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "drift-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = ["click>=8.0"]\n'
    )
    result = subprocess.run(
        ["uv", "sync", "--locked", "--no-dev", "--inexact"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0
    assert "needs to be updated" in result.stdout + result.stderr


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
    missing an import. Scans the import surface statically (first dotted
    component per statement; an indirect chain through a first-party module is
    out of scope — it would surface as ModuleNotFoundError at `ava start` and
    is caught by the readiness gate)."""
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
