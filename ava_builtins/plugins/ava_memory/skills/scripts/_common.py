"""Shared helpers for the ava-memory pool operation scripts.

Self-contained (stdlib + subprocess only): these scripts are meant to be
copied with the skill and run on any machine that has the pool checkout,
git, gh, and the `ava` CLI on PATH. No dependency on the Ava source tree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def ava_home() -> Path:
    return Path(os.environ.get("AVA_HOME", Path.home() / ".ava"))


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


def repo_slug(pool: Path) -> str:
    """user/repo from the pool's origin remote."""
    r = subprocess.run(
        ["git", "-C", str(pool), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"✗ cannot read origin remote: {r.stderr.strip()}")
    url = r.stdout.strip()
    # git@github.com:user/repo.git | https://github.com/user/repo.git
    return url.rstrip("/").rstrip(".git").split("github.com", 1)[-1].strip(":/")


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
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
