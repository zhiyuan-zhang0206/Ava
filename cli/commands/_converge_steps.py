"""Early host-wiring and unit-state converge steps."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared.paths import repo_root
from shared.platform_backend import get_backend
from shared.private_storage import converge_private_tree, ensure_private_file

# --- host-wiring steps (no preconditions) ---------------------------------


def _ensure_ava_symlink(ctx: ConvergeCtx) -> None:
    # On Windows the `ava` entry point is `.venv\Scripts\ava.exe`, reached via the
    # uv/venv on PATH (or the install.ps1 shim) — there is no `~/.local/bin/ava`
    # symlink model, and symlink creation needs admin/dev-mode. Skip.
    if not get_backend().supports_ava_symlink():
        return
    target = ctx.repo / ".venv" / "bin" / "ava"
    link = Path.home() / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.readlink() == target:
        return
    link.unlink(missing_ok=True)
    link.symlink_to(target)


_PATH_BEGIN = "# >>> ava path >>>"
_PATH_END = "# <<< ava path <<<"


def _shell_rc_path() -> Path:
    return Path.home() / (".zshrc" if os.environ.get("SHELL", "").endswith("zsh") else ".bashrc")


def _ensure_local_bin_on_path(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    # POSIX shell-rc PATH wiring; Windows uses a different PATH model (the
    # install.ps1 shim / venv Scripts on PATH), so there is no .bashrc to edit.
    if not get_backend().supports_shell_rc():
        return
    local_bin = Path.home() / ".local" / "bin"
    rc = _shell_rc_path()
    line = f'case ":$PATH:" in *":{local_bin}:"*) ;; *) export PATH="{local_bin}:$PATH" ;; esac'
    block = f"{_PATH_BEGIN}\n{line}\n{_PATH_END}"
    existing = rc.read_text() if rc.exists() else ""
    pattern = re.compile(re.escape(_PATH_BEGIN) + r".*?" + re.escape(_PATH_END), re.DOTALL)
    if pattern.search(existing):
        new = pattern.sub(block, existing)
    else:
        sep = "" if existing == "" or existing.endswith("\n") else "\n"
        new = f"{existing}{sep}{block}\n"
    if new != existing:
        rc.write_text(new)


def _ensure_ava_home_dirs(ctx: ConvergeCtx) -> None:
    for sub in ("configs", "secrets"):
        (ctx.ava_home / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "workspaces", "memory"):
        converge_private_tree(ctx.ava_home / sub)
    marker = ctx.ava_home / "logs" / ".metadata_never_index"
    marker.touch(exist_ok=True)
    ensure_private_file(marker)


def _ensure_pg_binaries_step(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Prepare the selected PostgreSQL 17 runtime and its pgvector extension."""
    from shared.config import settings

    if not get_backend().supports_data_plane() or settings.data_plane.is_remote:
        return
    from shared.pg_runtime import ensure_pg_runtime

    ensure_pg_runtime()


def ensure_local_git_hooks(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Warn when a conventional local checkout's Git hook installation is
    missing or drifted; never repair or block start.

    Runs the stdlib-only checker (``scripts/provision/check_git_hooks.py
    --scan-machine``) from THIS checkout and mirrors its ``WARNING`` lines to
    stderr. A checkout that predates the flag (usage error), a missing script,
    or any subprocess failure degrades to silence — a warn-only assertion must
    never make converge noisy or fatal on its own account.
    """
    script = repo_root() / "scripts" / "provision" / "check_git_hooks.py"
    if not script.exists():
        return
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--scan-machine"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode not in (0, 1):
        return  # pre-flag checkout or unexpected failure: stay silent
    for line in result.stdout.splitlines():
        if line.startswith("WARNING:"):
            print(f"  ! hooks: {line.removeprefix('WARNING: ').strip()}", file=sys.stderr)


# --- unit-state steps (need a configured unit) ----------------------------
