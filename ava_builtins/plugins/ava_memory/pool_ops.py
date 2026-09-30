"""Shared helpers for the ava-memory pool operation scripts.

Imported by the consolidation scripts under `skills/scripts/` (`consolidate.py`,
`steward.py`, `arbiter_merge.py`, `gen_indexes.py`, `rebuild_memory_index.py`)
as `ava_builtins.plugins.ava_memory.pool_ops`, so a run needs the checkout's
venv (`ava_builtins` importable) — `consolidation/SKILL.md` invokes them with
a bare `python`, which an agent's shell resolves to that venv (the venv's
`bin/` leads the PATH every agent process inherits). Stdlib + subprocess plus
the leaf that resolves the home (`base.host.env.dotenv_boot.resolve_ava_home`:
`$AVA_HOME`, else `~/.ava`), which builds no Settings.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from base.host.env.dotenv_boot import resolve_ava_home


def pool_dir() -> Path:
    return resolve_ava_home() / "memory"


def machine_name() -> str:
    env = os.environ.get("AVA_MACHINE_NAME")
    if env:
        return env
    mf = resolve_ava_home() / "machine_name"
    if mf.exists():
        return mf.read_text().strip()
    return "unknown"


def branch_name() -> str:
    return f"machine-{machine_name()}"


def current_branch(pool: Path) -> str:
    """The branch checked out in the pool ("" when HEAD is detached)."""
    r = subprocess.run(  # noqa: S603 — fixed git argv, static args, not shell-interpolated
        ["git", "-C", str(pool), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise SystemExit(f"✗ cannot read current branch: {r.stderr.strip()}")
    return r.stdout.strip()


def repo_slug(pool: Path) -> str:
    """user/repo from the pool's origin remote."""
    r = subprocess.run(  # noqa: S603 — fixed git argv, static args, not shell-interpolated
        ["git", "-C", str(pool), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise SystemExit(f"✗ cannot read origin remote: {r.stderr.strip()}")
    url = r.stdout.strip()
    # git@github.com:user/repo.git | https://github.com/user/repo.git
    return url.rstrip("/").rstrip(".git").split("github.com", 1)[-1].strip(":/")


def run(
    cmd: list[str], cwd: str | None = None, *, check: bool = True
) -> subprocess.CompletedProcess:
    r = subprocess.run(  # noqa: S603 — caller-constructed argv, static args, not shell-interpolated
        cmd, capture_output=True, text=True, cwd=cwd, check=False
    )
    if r.stdout:
        print(r.stdout.rstrip())
    if check and r.returncode != 0:
        raise SystemExit(f"✗ {' '.join(cmd)} failed: {r.stderr.strip()}")
    return r


def stage_and_commit(message: str, pool: Path) -> bool:
    run(["git", "-C", str(pool), "add", "-A"])
    status = run(["git", "-C", str(pool), "status", "--porcelain"], check=False)
    if not status.stdout.strip():
        print("  (nothing to commit)")
        return False
    run(["git", "-C", str(pool), "commit", "-m", message])
    print(f"  committed: {message}")
    return True


def refresh_index() -> None:
    """Refresh the gateway's memory index via the CLI (the one memory CLI that stays)."""
    run(["ava", "memory", "refresh"])
