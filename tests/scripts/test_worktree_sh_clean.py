"""Process-level checks for `scripts/worktree.sh clean` (task #3707, issue #194).

The live-anchor checker itself is covered by tests/shared/test_worktree_guard.py;
these tests exercise the shell wrapper: that it refuses on an anchored worktree
or when the checker cannot run at all, and that it never escalates to a forced
removal without an explicit --force.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — git under the test's own fixtures
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _clean(repo: Path, home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the real script under a deliberate environment, never the host's venv."""
    env = {"PATH": os.environ["PATH"], "HOME": str(home.parent), "AVA_HOME": str(home)}
    return subprocess.run(  # noqa: S603 — repository-owned script, deliberate environment
        ["bash", str(repo / "scripts" / "worktree.sh"), "clean", *extra],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_repo(tmp_path: Path) -> Path:
    """A throwaway repo shaped like the dev clone: the script + its checker."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("worktree.sh", "check_worktree_remove.py"):
        (repo / "scripts" / name).write_bytes((_REPO_ROOT / "scripts" / name).read_bytes())
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@ava",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    return repo


def _add_worktree(repo: Path) -> Path:
    """The worktree cmd_clean targets, with the branch it deletes."""
    target = repo / ".worktrees" / "t1"
    _git(repo, "worktree", "add", "-q", ".worktrees/t1", "-b", "ava/t1")
    return target


def _plant_usable_python(repo: Path) -> None:
    """Shim the script accepts as python: -x plus a working `import psutil`.

    The script's resolution is what is under test; the shim is the running
    interpreter (psutil included), so the checker itself really runs.
    """
    shim = repo / ".venv" / "bin" / "python"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)


def _fake_home(tmp_path: Path, *, record_cwd: Path | None = None) -> Path:
    """An $AVA_HOME whose pty records hold one session (the anchor)."""
    home = tmp_path / "ava-home"
    records = home / "run" / "pty"
    records.mkdir(parents=True)
    if record_cwd is not None:
        (records / "1.json").write_text(json.dumps({"cwd": str(record_cwd), "pid": 424242}))
    return home


def _branch_exists(repo: Path, name: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0


def test_anchored_worktree_is_refused_and_force_overrides(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = _add_worktree(repo)
    _plant_usable_python(repo)
    home = _fake_home(tmp_path, record_cwd=target)

    refused = _clean(repo, home, "t1")

    assert refused.returncode == 1
    assert "REFUSE" in refused.stderr
    assert "removal refused" in refused.stderr
    assert "live-anchor check passed" not in refused.stdout
    assert target.is_dir()
    assert _branch_exists(repo, "ava/t1")

    forced = _clean(repo, home, "t1", "--force")

    assert forced.returncode == 0, forced.stderr
    assert "removing anyway (--force)" in forced.stderr
    assert not target.exists()
    assert not _branch_exists(repo, "ava/t1")


def test_unusable_checker_environment_refuses_and_force_overrides(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = _add_worktree(repo)
    home = _fake_home(tmp_path)  # no .venv anywhere for the checker to run

    refused = _clean(repo, home, "t1")

    assert refused.returncode == 1
    assert "no python with psutil" in refused.stderr
    assert target.is_dir()
    assert _branch_exists(repo, "ava/t1")

    forced = _clean(repo, home, "t1", "--force")

    assert forced.returncode == 0, forced.stderr
    assert "SKIPPED (--force)" in forced.stderr
    assert not target.exists()
    assert not _branch_exists(repo, "ava/t1")


def test_missing_checker_refuses_and_force_overrides(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = _add_worktree(repo)
    (repo / "scripts" / "check_worktree_remove.py").unlink()
    home = _fake_home(tmp_path)

    refused = _clean(repo, home, "t1")

    assert refused.returncode == 1
    assert "checker missing" in refused.stderr

    forced = _clean(repo, home, "t1", "--force")

    assert forced.returncode == 0, forced.stderr
    assert "SKIPPED (--force)" in forced.stderr
    assert not target.exists()


def test_dirty_worktree_is_kept_without_force(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = _add_worktree(repo)
    _plant_usable_python(repo)
    home = _fake_home(tmp_path)
    junk = target / "junk.txt"
    junk.write_text("uncommitted")

    refused = _clean(repo, home, "t1")

    assert refused.returncode == 1
    assert "removal failed" in refused.stderr
    assert junk.exists()
    assert _branch_exists(repo, "ava/t1")

    forced = _clean(repo, home, "t1", "--force")

    assert forced.returncode == 0, forced.stderr
    assert "force-removing worktree (--force)" in forced.stdout
    assert not target.exists()
    assert not _branch_exists(repo, "ava/t1")


def test_clean_worktree_is_removed_and_branch_deleted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = _add_worktree(repo)
    _plant_usable_python(repo)
    home = _fake_home(tmp_path)

    done = _clean(repo, home, "t1")

    assert done.returncode == 0, done.stderr
    assert "live-anchor check passed" in done.stdout
    assert "worktree removed" in done.stdout
    assert "branch deleted: ava/t1" in done.stdout
    assert not target.exists()
    assert not _branch_exists(repo, "ava/t1")


def test_option_errors_are_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    home = _fake_home(tmp_path)

    missing = _clean(repo, home)
    assert missing.returncode != 0
    assert "usage" in missing.stderr.lower()

    unknown = _clean(repo, home, "t1", "--bogus")
    assert unknown.returncode == 1
    assert "unknown option: --bogus" in unknown.stderr

    excess = _clean(repo, home, "t1", "--force", "extra")
    assert excess.returncode == 1
    assert "too many arguments" in excess.stderr
