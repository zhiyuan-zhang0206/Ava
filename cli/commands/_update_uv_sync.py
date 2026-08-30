"""Native update-chain seam for a production-scoped ``uv sync``.

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

import os
import subprocess
import sys
from pathlib import Path

from cli.commands._repo import _repo_root
from shared.deploy_timing import UV_SYNC_TIMEOUT_S
from shared.editable_install import editable_pth_write_window
from shared.paths import ava_home
from shared.platform import IS_WINDOWS
from shared.proc import run_bounded

_PROD_SYNC_ARGS = ["uv", "sync", "--locked", "--no-dev", "--inexact", "--verbose"]


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


def run_uv_sync(repo: Path) -> subprocess.CompletedProcess[bytes]:
    """Run production ``uv sync`` while preserving the editable pointer's exact mode.

    Bounded by `UV_SYNC_TIMEOUT_S` through `shared.proc.run_bounded` — the bound
    kills the whole process tree, not just the process Python spawned
    (``subprocess.run(timeout=...)`` leaves descendants alive; see shared/proc.py).
    A hung sync is converted into a failed result rather than raised, so every
    caller keeps its existing ``returncode != 0`` handling and the updater chain
    ends in a terminal, diagnosable outcome instead of silently stalling the
    rollout. The same applies to a sync that cannot start at all (uv missing) —
    both print the reason to stderr, which lands in the updater log.
    """
    cache_dir = _uv_cache_dir()
    if cache_dir is not None:
        os.environ.setdefault("UV_CACHE_DIR", cache_dir)
    with editable_pth_write_window(repo):
        try:
            return run_bounded(
                _PROD_SYNC_ARGS,
                cwd=repo,
                capture_output=False,
                timeout=UV_SYNC_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            print(
                f"  ✗ uv sync timed out after {UV_SYNC_TIMEOUT_S:.0f}s — the sync stopped "
                "making progress (a stalled download or a wedged uv); its process tree "
                "was killed. The verbose lines above show the last package uv was "
                "downloading.",
                file=sys.stderr,
            )
            return subprocess.CompletedProcess(
                _PROD_SYNC_ARGS, returncode=124, stdout=None, stderr=None
            )
        except OSError as exc:
            print(
                f"  ✗ uv sync could not start: {exc} — is uv installed and on PATH?",
                file=sys.stderr,
            )
            return subprocess.CompletedProcess(
                _PROD_SYNC_ARGS, returncode=127, stdout=None, stderr=None
            )


def _main() -> int:
    repo = _repo_root()
    return run_uv_sync(repo).returncode


if __name__ == "__main__":
    raise SystemExit(_main())
