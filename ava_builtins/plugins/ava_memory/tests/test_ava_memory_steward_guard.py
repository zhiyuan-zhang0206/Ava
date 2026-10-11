"""steward.py guard: refuse to run outside this machine's own branch (task #3452).

The steward pushes whatever branch the pool checkout is on, so a run from
`main` pushes straight to main (observed 2026-09-14: a batch commit landed on
origin/main without a PR). The guard must stop the run before anything is
staged, committed, or pushed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_STEWARD = (
    _REPO_ROOT / "ava_builtins" / "plugins" / "ava_memory" / "skills" / "scripts" / "steward.py"
)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout


def _make_pool(tmp_path: Path) -> Path:
    pool = tmp_path / ".ava" / "memory"
    pool.mkdir(parents=True)
    _git(pool, "init", "-q", "-b", "main")
    _git(pool, "config", "user.email", "steward-test@example.invalid")
    _git(pool, "config", "user.name", "Steward Test")
    (pool / "seed.md").write_text("seed\n")
    _git(pool, "add", "-A")
    _git(pool, "commit", "-qm", "init")
    return pool


def _run_steward(pool: Path, path_extra: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AVA_HOME"] = str(pool.parent)
    env["AVA_MACHINE_NAME"] = "testbox"
    if path_extra:
        env["PATH"] = path_extra + os.pathsep + env["PATH"]
    return subprocess.run(  # noqa: S603 — sys.executable + repository-owned script
        [sys.executable, str(_STEWARD), "-m", "memory: testbox test - guard"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_refuses_outside_machine_branch(tmp_path: Path) -> None:
    pool = _make_pool(tmp_path)
    (pool / "pending.md").write_text("pending\n")

    res = _run_steward(pool)

    output = res.stdout + res.stderr
    assert res.returncode != 0
    assert "refusing" in output
    assert "machine-testbox" in output
    # The refusal happens before any write: no commit, no staged change, file intact.
    assert len(_git(pool, "log", "--oneline").splitlines()) == 1
    assert _git(pool, "ls-files").strip() == "seed.md"
    assert (pool / "pending.md").read_text() == "pending\n"


def test_proceeds_on_machine_branch(tmp_path: Path) -> None:
    pool = _make_pool(tmp_path)
    _git(pool, "checkout", "-qb", "machine-testbox")
    (pool / "pending.md").write_text("pending\n")

    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "--bare", "-q", str(remote)], check=True
    )
    _git(pool, "remote", "add", "origin", str(remote))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\necho "https://example.invalid/pr/1"\n')
    gh.chmod(0o755)

    res = _run_steward(pool, path_extra=str(bindir))

    assert res.returncode == 0, res.stderr
    assert len(_git(pool, "log", "--oneline").splitlines()) == 2
    heads = _git(pool, "ls-remote", "--heads", "origin")
    assert "refs/heads/machine-testbox" in heads
    assert "refs/heads/main" not in heads


_NOTE = (
    "---\ntype: Memory\nava_agent: 7\ntitle: Retry rule\ndescription: Retries above three hurt\n"
    "tags: [type/feedback]\n---\nBody.\n"
)


def test_directory_indexes_are_rebuilt_before_the_commit(tmp_path: Path) -> None:
    """Notes arrive written with `ava.files`, with no index entry; the steward's commit carries
    the rebuilt indexes so the pool validator's pointer checks have something to check."""
    pool = _make_pool(tmp_path)
    _git(pool, "checkout", "-qb", "machine-testbox")
    (pool / "topics").mkdir()
    (pool / "topics" / "retry.md").write_text(_NOTE)
    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "--bare", "-q", str(remote)], check=True
    )
    _git(pool, "remote", "add", "origin", str(remote))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\necho "https://example.invalid/pr/1"\n')
    gh.chmod(0o755)

    res = _run_steward(pool, path_extra=str(bindir))

    assert res.returncode == 0, res.stderr
    assert "* [Retry rule](retry.md) - Retries above three hurt" in _git(
        pool, "show", "HEAD:topics/index.md"
    )
    assert "* [topics/](topics/) - 1 notes" in _git(pool, "show", "HEAD:index.md")
    assert _git(pool, "status", "--porcelain").strip() == ""


def test_regenerate_indexes_writes_every_directory_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ava_builtins.plugins.ava_memory import pool_ops

    pool = tmp_path / "memory"
    (pool / "a" / "b").mkdir(parents=True)
    (pool / "a" / "b" / "deep.md").write_text(_NOTE)
    monkeypatch.setenv("AVA_HOME", str(tmp_path))

    pool_ops.regenerate_indexes()

    assert "* [Retry rule](deep.md)" in (pool / "a" / "b" / "index.md").read_text()
    assert "* [b/](b/) - 1 notes" in (pool / "a" / "index.md").read_text()
    assert (pool / "index.md").is_file()


def _run_validator(pool: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — sys.executable + the validator copy seeded in the pool
        [sys.executable, str(pool / "validate.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_new_root_note_passes_the_pool_validator_after_the_steward(tmp_path: Path) -> None:
    """Nothing maintains `MEMORY.md` pointers as notes are written with `ava.files`: the steward's
    rebuild adds the root note's pointer and the directory's, so the pool validator (the pre-commit
    hook's check) passes on what is committed."""
    import shutil

    template = _REPO_ROOT / "ava_builtins" / "plugins" / "ava_memory" / "template"
    pool = tmp_path / ".ava" / "memory"
    pool.mkdir(parents=True)
    _git(pool, "init", "-q", "-b", "main")
    _git(pool, "config", "user.email", "steward-test@example.invalid")
    _git(pool, "config", "user.name", "Steward Test")
    shutil.copy(template / "MEMORY.md", pool / "MEMORY.md")
    shutil.copy(template / "validate.py", pool / "validate.py")
    _git(pool, "add", "-A")
    _git(pool, "commit", "-qm", "seed")
    _git(pool, "checkout", "-qb", "machine-testbox")
    (pool / "rule.md").write_text(_NOTE)
    (pool / "topics").mkdir()
    (pool / "topics" / "retry.md").write_text(_NOTE.replace("Retry rule", "Topic rule"))
    assert _run_validator(pool).returncode != 0  # the new notes are orphans until indexed

    remote = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "--bare", "-q", str(remote)], check=True
    )
    _git(pool, "remote", "add", "origin", str(remote))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\necho "https://example.invalid/pr/1"\n')
    gh.chmod(0o755)

    res = _run_steward(pool, path_extra=str(bindir))

    assert res.returncode == 0, res.stderr
    memory_index = (pool / "MEMORY.md").read_text()
    assert "## Setup (keep current, do not drop)" in memory_index
    assert "- [Retry rule](rule.md) — Retries above three hurt" in memory_index
    assert "- [topics/](topics/index.md) — 1 notes" in memory_index
    validated = _run_validator(pool)
    assert validated.returncode == 0, validated.stderr
    assert _git(pool, "status", "--porcelain").strip() == ""
