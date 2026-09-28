"""Shared helpers for the ava-memory pool operation scripts.

Imported by the consolidation scripts under `skills/scripts/` (`consolidate.py`,
`steward.py`, `arbiter_merge.py`, `gen_indexes.py`, `rebuild_memory_index.py`)
as `ava_builtins.plugins.ava_memory.pool_ops`, so a run needs the checkout's
venv (`ava_builtins` importable) — `consolidation/SKILL.md` invokes them with
a bare `python`, which an agent's shell resolves to that venv (the venv's
`bin/` leads the PATH every agent process inherits). Still stdlib + subprocess only, no
`shared` import: `ava_home()` does not use the checkout-anchored home
resolution the rest of the repo uses (`shared.dotenv_boot`), for the reason
below.

`ava_home()` takes the opposite, simpler stance: require an explicit
`AVA_HOME` instead of guessing one. Every legitimate caller already has it:
these scripts run inside an agent's shell tool, a child of the agent process
that pinned `AVA_HOME` into its own environment at boot
(`shared.dotenv_boot.load_ava_env`), which subprocess inherits. A caller with
no `AVA_HOME` — an ad-hoc run from an unrelated shell, e.g. a dev checkout
with no cluster of its own — has no business guessing `~/.ava` either: that
default is THIS MACHINE's real cluster home, and `pool_dir()` /
`refresh_index()` are write paths (git commit + push to the pool, `ava
memory refresh`), so a wrong guess here does not just misread — it can
mutate production (the same "unanchored checkout reaches production" bug
class as `shared/dotenv_boot.py`, 2026-09-27).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def ava_home() -> Path:
    """This process's Ava home. No fallback — see the module docstring."""
    env = os.environ.get("AVA_HOME")
    if not env:
        raise SystemExit(
            "AVA_HOME is not set. These scripts never guess a home (an agent's shell "
            "inherits it from the agent process; a manual run must set it explicitly) "
            "-- pass AVA_HOME=<path> instead of relying on ~/.ava."
        )
    return Path(env)


def pool_dir() -> Path:
    return ava_home() / "memory"


def machine_name() -> str:
    env = os.environ.get("AVA_MACHINE_NAME")
    if env:
        return env
    mf = ava_home() / "machine_name"
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
