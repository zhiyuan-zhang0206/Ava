"""Native update-chain seam for a production-scoped locked Python install.

``cli.python_install`` is imported before checkout and shared with install.sh.
Its stdlib-only dependency closure stays callable when rollback removes the new
helper from disk. It uses native ``uv sync`` for PyPI, or locked export plus
hash-checked ``uv pip`` installation for a host mirror. Every uv process tree
shares one deadline inside the editable write window, with record restoration
and post-install import proof around both paths.

The bare ``uv sync`` this module used to run syncs EVERY dependency group —
including the ``dev`` group (pyright / pytest / ruff / ...). On the 2026-08-30
rollout the Windows agent-runner spent 449 s downloading the pyright 1.1.411
wheel (uv log stuck at "Downloading pyright (5.9MiB)", RAM at 97.3%) and held
the whole rollout behind it. Production sync must never install dev-only
dependencies; the flag set below is the fix, plus the robustness requirements
the stall exposed:

- ``--no-dev`` removes the dev group from the sync target set, so no dev wheel
  is ever downloaded by an update. Runtime dependencies stay complete (the
  project + non-dev groups remain the target set).
- ``--locked`` asserts lockfile freshness: the sync fails with an actionable
  error when uv.lock drifts from pyproject.toml (``--frozen`` alone only skips
  lockfile *updates* — uv's default — and would silently install the stale
  lock's environment, exactly the silent-surprise class this seam exists to
  kill). A lock change lands through the deliberate ``uv lock`` + rollout
  flow, never silently mid-update; the committed lockfile stays the single
  source of the runtime pins.
- ``--inexact`` matters in the other direction: sync reconciles the venv to
  the target set and would otherwise UNINSTALL the dev packages a host already
  has. Agent sandboxes run out of this very venv and reach for pytest / pyright
  / ruff through it, so removing them on the first update after this change
  would break every agent's local check workflow. ``--inexact`` leaves the
  already-installed dev packages alone: production sync stops installing dev
  deps without deleting the ones present.
- ``--verbose`` is the progress heartbeat. In a non-TTY session uv prints
  nothing between "Resolved N packages" and the final summary, so a slow
  download is indistinguishable from a dead process — exactly the shape the
  Windows stall took. Verbose mode emits a line per wheel download, which is
  how the updater log shows continuous progress during long downloads.

On Windows the uv cache is pinned to an explicit directory
(``%LOCALAPPDATA%\\ava\\uv-cache``) before the sync runs. uv's default Windows
cache (``%LOCALAPPDATA%\\uv\\cache``) is already stable; the pin is
defense-in-depth — the location is fixed regardless of uv defaults or the
Task-Scheduler context the updater runs in, and the ``ava_home`` fallback
covers a context without LOCALAPPDATA. POSIX keeps uv's default cache
location (already stable).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from cli.commands._repo import _repo_root
from cli.python_install import install as install_locked
from shared.deploy_timing import UV_SYNC_TIMEOUT_S
from shared.editable_install import (
    editable_ava_pth_paths,
    editable_direct_url_paths,
    editable_import_gate,
    editable_pth_write_window,
    editable_site_packages_dirs,
)
from shared.paths import ava_home
from shared.platform import IS_WINDOWS
from shared.platform_backend import get_backend
from shared.proc import run_bounded

_PROD_SYNC_ARGS = ["uv", "sync", "--locked", "--inexact", "--no-config", "--no-dev", "--verbose"]


@dataclass(frozen=True)
class _EditableRecordSnapshot:
    """One editable-install record's state before a production ``uv sync``."""

    path: Path
    exists: bool
    content: bytes | None


def _target_interpreter(repo: Path) -> str | None:
    """Pin an existing venv; fresh staging lets uv select a managed interpreter."""
    python_name = "python.exe" if IS_WINDOWS else "python"
    interpreter = repo / ".venv" / get_backend().venv_bin_dir_name() / python_name
    return str(interpreter) if interpreter.exists() else None


def _run_locked_install(
    repo: Path, timeout_s: float, reinstall_package: str | None
) -> subprocess.CompletedProcess[bytes]:
    """Use the already-imported installer even after checkout removes its files."""
    deadline = monotonic() + timeout_s
    last_result: subprocess.CompletedProcess[bytes] | None = None

    def run_step(
        argv: list[str], repo: Path, env: dict[str, str], *, discard_stdout: bool = False
    ) -> int:
        nonlocal last_result
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout_s)
        last_result = run_bounded(
            argv,
            cwd=repo,
            env=env,
            capture_output=False,
            stdout=subprocess.DEVNULL if discard_stdout else None,
            timeout=remaining,
        )
        return last_result.returncode

    install_locked(
        repo,
        no_dev=True,
        verbose=True,
        interpreter=_target_interpreter(repo),
        reinstall_package=reinstall_package,
        run=run_step,
    )
    if last_result is None:
        raise RuntimeError("Locked installer returned without running uv")
    return last_result


def _uv_cache_dir() -> str | None:
    """Stable uv cache location for production sync, or None to keep uv's default.

    The Windows agent-runner's updater runs under the Task Scheduler; pinning
    the cache to an explicit path (``%LOCALAPPDATA%\\ava\\uv-cache``, falling
    back to the ava home) fixes the location regardless of uv defaults or the
    scheduler context (defense-in-depth — uv's default Windows cache is
    already stable). POSIX returns None — uv's default (``~/.cache/uv``) is
    already a stable, non-temp directory.
    """
    if not IS_WINDOWS:
        return None
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return str(Path(local) / "ava" / "uv-cache")
    return str(ava_home() / "uv-cache")


def _failed_uv_sync(returncode: int) -> subprocess.CompletedProcess[bytes]:
    """Create the terminal result used by sync preflight and launch failures."""

    return subprocess.CompletedProcess(
        _PROD_SYNC_ARGS, returncode=returncode, stdout=None, stderr=None
    )


def _preflight_editable_site_packages(repo: Path) -> subprocess.CompletedProcess[bytes] | None:
    """Probe uv's write, rename, and unlink boundary before it changes the venv."""

    for directory in editable_site_packages_dirs(repo):
        probe = directory / f".ava-write-probe-{os.getpid()}"
        replacement = directory / f".{probe.name}.tmp"
        failure: tuple[str, OSError] | None = None
        operation = "write"
        try:
            probe.write_text("")
            operation = "rename"
            probe.rename(replacement)
            operation = "unlink"
            replacement.unlink()
        except OSError as exc:
            failure = (operation, exc)
        finally:
            for leftover in (probe, replacement):
                try:
                    leftover.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if failure is None:
                        failure = ("cleanup unlink", exc)
                    else:
                        print(
                            f"  ! uv sync preflight could not clean up {leftover}: {exc}",
                            file=sys.stderr,
                        )
        if failure is not None:
            operation, exc = failure
            print(
                f"  ✗ uv sync preflight failed in {directory}: {operation} "
                f"operation on the editable pointer would fail: {exc}",
                file=sys.stderr,
            )
            return _failed_uv_sync(126)
    return None


def _snapshot_editable_records(repo: Path) -> tuple[_EditableRecordSnapshot, ...]:
    """Capture every current Ava pointer and direct-URL record before uv mutates it."""

    snapshots: list[_EditableRecordSnapshot] = []
    records = editable_ava_pth_paths(repo) + editable_direct_url_paths(repo)
    for path in records:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            snapshots.append(_EditableRecordSnapshot(path, exists=False, content=None))
        else:
            snapshots.append(_EditableRecordSnapshot(path, exists=True, content=content))
    return tuple(snapshots)


def _restore_record(path: Path, content: bytes) -> None:
    """Restore one record through uv's same-directory atomic replacement shape."""

    temporary = path.with_name(f".{path.name}.ava-restore-{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _restore_editable_records(snapshots: tuple[_EditableRecordSnapshot, ...]) -> None:
    """Return altered pre-sync editable records to their exact byte content."""

    for snapshot in snapshots:
        if not snapshot.exists or snapshot.content is None:
            continue
        try:
            unchanged = snapshot.path.read_bytes() == snapshot.content
        except OSError:
            unchanged = False
        if unchanged:
            continue
        try:
            snapshot.path.parent.mkdir(parents=True, exist_ok=True)
            _restore_record(snapshot.path, snapshot.content)
        except OSError as exc:
            print(
                f"  ! failed to restore editable install record {snapshot.path} "
                f"after uv sync failure: {exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"  ! restored editable install record {snapshot.path} after uv sync failure",
                file=sys.stderr,
            )


def run_uv_sync(
    repo: Path,
    *,
    timeout_s: float = UV_SYNC_TIMEOUT_S,
    reinstall_package: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run production ``uv sync`` while preserving the editable pointer's exact mode.

    Bounded by `UV_SYNC_TIMEOUT_S` through `shared.proc.run_bounded` — the bound
    kills the whole process tree, not just the process Python spawned
    (``subprocess.run(timeout=...)`` leaves descendants alive; see shared/proc.py).
    A hung sync is converted into a failed result rather than raised, so every
    caller keeps its existing ``returncode != 0`` handling and the updater chain
    ends in a terminal, diagnosable outcome instead of silently stalling the
    rollout. The same applies to a sync that cannot start at all (uv missing) —
    both print the reason to stderr, which lands in the updater log. Before uv
    starts, its site-packages write boundary is probed with the same write,
    rename, and unlink sequence it uses for the editable pointer. A failed
    sync restores any changed or missing editable records to their exact
    pre-sync bytes before the write window closes.
    """
    cache_dir = _uv_cache_dir()
    if cache_dir is not None:
        os.environ.setdefault("UV_CACHE_DIR", cache_dir)
    with editable_pth_write_window(repo):
        preflight_failure = _preflight_editable_site_packages(repo)
        if preflight_failure is not None:
            return preflight_failure
        try:
            snapshots = _snapshot_editable_records(repo)
        except OSError as exc:
            print(
                f"  ✗ uv sync could not snapshot editable install records: {exc}",
                file=sys.stderr,
            )
            return _failed_uv_sync(126)
        try:
            result = _run_locked_install(repo, timeout_s, reinstall_package)
        except subprocess.TimeoutExpired:
            print(
                f"  ✗ uv sync timed out after {timeout_s:.0f}s — the sync stopped "
                "making progress (a stalled download or a wedged uv); its process tree "
                "was killed. The verbose lines above show the last package uv was "
                "downloading.",
                file=sys.stderr,
            )
            result = _failed_uv_sync(124)
        except ValueError as exc:
            print(f"  ✗ locked Python install refused: {exc}", file=sys.stderr)
            result = _failed_uv_sync(126)
        except OSError as exc:
            print(
                f"  ✗ uv sync could not start: {exc} — is uv installed and on PATH?",
                file=sys.stderr,
            )
            result = _failed_uv_sync(127)
        if result.returncode != 0:
            _restore_editable_records(snapshots)
        return result


def run_uv_sync_verified(
    repo: Path, *, timeout_s: float = UV_SYNC_TIMEOUT_S
) -> subprocess.CompletedProcess[bytes]:
    """Run production sync, then prove its venv imports the checked-out agent code."""

    result = run_uv_sync(repo, timeout_s=timeout_s)
    if result.returncode != 0:
        return result
    violations = editable_import_gate(repo)
    if not violations:
        return result
    return subprocess.CompletedProcess(
        result.args,
        returncode=126,
        stdout=result.stdout,
        stderr="\n".join(violations).encode(),
    )


def _main() -> int:
    repo = _repo_root()
    return run_uv_sync(repo).returncode


if __name__ == "__main__":
    raise SystemExit(_main())
