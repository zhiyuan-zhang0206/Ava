from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRANCH_LINT = ROOT / "scripts/hooks/prepush-branch-lint.sh"
MODULE = ROOT / "scripts/provision/prepush_branch_diff.py"


def _init_probe_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def _init_repo_with_origin_main(path: Path) -> str:
    _init_probe_repo(path)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)
    base_sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", base_sha], check=True
    )
    return base_sha


def _venv(path: Path, pre_commit: str) -> None:
    bin_dir = path / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    (bin_dir / "pre-commit").write_text(pre_commit)
    (bin_dir / "pre-commit").chmod(0o755)


def _install_module(path: Path) -> None:
    (path / "scripts/provision").mkdir(parents=True, exist_ok=True)
    shutil.copy(MODULE, path / "scripts/provision/prepush_branch_diff.py")


def _hook(name: str, entry: str) -> str:
    return (
        f"      - id: {name}\n"
        f"        name: {name}\n"
        f"        entry: {entry}\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n"
    )


def _branch_repo(path: Path, hooks: str) -> None:
    _init_repo_with_origin_main(path)
    (path / ".gitignore").write_text(".venv/\n")
    (path / ".pre-commit-config.yaml").write_text(f"repos:\n  - repo: local\n    hooks:\n{hooks}")
    (path / "trimmed.txt").write_text("committed\n")
    _venv(path, f'#!/bin/sh\nexec "{sys.executable}" -m pre_commit "$@"\n')
    _install_module(path)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "branch"], check=True)


def _branch_lint(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(BRANCH_LINT)],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_branch_lint_judges_head_without_touching_the_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _branch_repo(
        repo,
        _hook("fixer", "bash -c 'echo appended >> trimmed.txt'")
        + _hook("clean-tree", "bash -c 'git diff --quiet -- || exit 2'")
        + _hook("content", "bash -c 'echo content failure; exit 1'"),
    )

    result = _branch_lint(repo)

    assert result.returncode == 1
    assert "fixer: rewrote 1 file(s): trimmed.txt" in result.stderr
    assert "content: failed" in result.stderr
    assert "clean-tree:" not in result.stderr
    assert "note: clean-tree failed in the first pass only" in result.stderr
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""
    worktrees = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(worktrees.splitlines()) == 1


def test_branch_lint_passes_when_the_committed_text_is_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _branch_repo(repo, _hook("tidy", "bash -c 'exit 0'"))

    result = _branch_lint(repo)

    assert result.returncode == 0, result.stderr
    assert "branch diff lint:" not in result.stderr


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_branch_lint_reads_head_not_the_working_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    record = tmp_path / "record.txt"
    command = (
        f"bash -c 'cat trimmed.txt >> {record}; "
        f"test -e untracked.txt && echo untracked-present >> {record} "
        f"|| echo untracked-absent >> {record}'"
    )
    _branch_repo(repo, _hook("record", command))
    (repo / "trimmed.txt").write_text("staged\n")
    subprocess.run(["git", "-C", str(repo), "add", "trimmed.txt"], check=True)
    (repo / "trimmed.txt").write_text("worktree\n")
    (repo / "README.md").write_text("staged-readme\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    (repo / "untracked.txt").write_text("untracked\n")
    before = _status(repo)

    result = _branch_lint(repo)

    assert result.returncode == 0, result.stderr
    assert record.read_text() == "committed\nuntracked-absent\n"
    assert _status(repo) == before


def test_branch_lint_names_a_hook_that_failed_and_rewrote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _branch_repo(repo, _hook("both", "bash -c 'echo extra >> trimmed.txt; echo bad; exit 1'"))

    result = _branch_lint(repo)

    assert result.returncode == 1
    assert "both: failed and rewrote 1 file(s): trimmed.txt" in result.stderr
