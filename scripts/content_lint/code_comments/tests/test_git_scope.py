import os
import subprocess
from pathlib import Path

import pytest

from base.host.proc import run_bounded

SCOPE = Path(__file__).resolve().parents[1] / "changed.sh"


def run(
    root: Path, *argv: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return run_bounded(list(argv), cwd=root, env=env, timeout=20, text=True, capture_output=True)


def git(root: Path, *args: str) -> str:
    result = run(root, "git", *args)
    result.check_returncode()
    return result.stdout.strip()


def initialize(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "comment-check@example.invalid")
    git(root, "config", "user.name", "comment-check")


@pytest.mark.parametrize("symlink_first", [True, False])
def test_file_type_changes_are_included_in_staged_and_committed_scopes(
    tmp_path: Path, symlink_first: bool
) -> None:
    initialize(tmp_path)
    (tmp_path / "target").write_text("value = 1\n", encoding="utf-8")
    source = tmp_path / "core.py"
    if symlink_first:
        source.symlink_to("target")
    else:
        source.write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    source.unlink()
    if symlink_first:
        source.write_text("# Free prose\nvalue = 1\n", encoding="utf-8")
    else:
        source.symlink_to("target")
    git(tmp_path, "add", "core.py")
    staged = run(tmp_path, "bash", str(SCOPE))
    assert staged.returncode == 0
    assert staged.stdout == "core.py\0"
    git(tmp_path, "commit", "-qm", "change type")
    committed = run(tmp_path, "bash", str(SCOPE), base)
    assert committed.returncode == 0
    assert committed.stdout == "core.py\0"


def test_prepush_range_checks_committed_changes_with_an_empty_index(tmp_path: Path) -> None:
    initialize(tmp_path)
    source = tmp_path / "core.py"
    source.write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    source.write_text("# Added prose\nvalue = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "changed")
    result = run(
        tmp_path,
        "bash",
        str(SCOPE),
        env={**os.environ, "PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": "HEAD"},
    )
    assert result.returncode == 0
    assert result.stdout == "core.py\0"
    stale = run(
        tmp_path,
        "bash",
        str(SCOPE),
        env={**os.environ, "PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": base},
    )
    assert stale.returncode != 0
    assert "checked-out HEAD" in stale.stderr
    assert run(tmp_path, "bash", str(SCOPE), "missing-base").returncode != 0
