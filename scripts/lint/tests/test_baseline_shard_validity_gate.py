"""Gate-level coverage for malformed or missing scripts/structure/baseline/
shards, through lcs.main(): a malformed or duplicate shard is an actionable
error, and so is a missing shard directory — its README.md is what keeps git
tracking it (and comparable as a base revision) even with zero shards, so an
absent directory can only be an accidental deletion, not a legitimate empty
baseline. See test_baseline_shards.py for unit coverage of the
split/render/merge/read machinery itself, and test_lint_code_structure.py for
the rest of the gate."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards


def _clear_baseline_dir(root: pathlib.Path) -> pathlib.Path:
    """The shard directory, emptied of any shard files already there (created if
    absent) and carrying its README.md."""
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.rglob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    return directory


def _run(
    command: list[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed test commands, never external input.
        command, cwd=cwd, env=env, check=check, capture_output=True, text=True, input=input
    )


def _git(
    root: pathlib.Path, *args: str, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Structure gate test",
            "-c",
            "user.email=structure-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        input=input,
    )


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every main() call scans only its own temporary root."""
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD")


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "[]",
        '{"extra": {}}',
        '{"files": []}',
        '{"directories": []}',
        '{"files": {"tests/big.py": true}}',
        '{"files": {"tests/big.py": "801"}}',
        '{"files": {"tests/big.py": 801.5}}',
        '{"directories": {"tests": "21"}}',
        '{"files": {"tests/big.txt": 801}}',
        '{"files": {"../big.py": 801}}',
        '{"files": {"/tests/big.py": 801}}',
    ],
)
def test_malformed_baseline_shard_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    directory = _clear_baseline_dir(tmp_path)
    (directory / "tests.json").write_text(content, encoding="utf-8")
    assert lcs.main([], repo_root=tmp_path) == 1
    assert f"{baseline_shards.SHARD_DIR}: invalid baseline" in capsys.readouterr().err


def test_duplicate_baseline_entry_across_shards_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _clear_baseline_dir(tmp_path)
    (directory / "base.json").write_text(
        json.dumps({"ambient_state": {"base/q.py::import-time-call:atexit.register": 2}}),
        encoding="utf-8",
    )
    (directory / "tests.json").write_text(
        json.dumps({"ambient_state": {"base/q.py::import-time-call:atexit.register": 2}}),
        encoding="utf-8",
    )
    assert lcs.main([], repo_root=tmp_path) == 1
    captured = capsys.readouterr()
    assert f"{baseline_shards.SHARD_DIR}: invalid baseline" in captured.err
    assert (
        "duplicates ambient_state entry 'base/q.py::import-time-call:atexit.register'"
        in captured.err
    )


def test_missing_baseline_directory_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No shard directory (not even its README.md) can only be an accidental
    deletion — read_worktree() fails fast rather than silently reading it as an
    empty baseline."""
    assert lcs.main([], repo_root=tmp_path) == 1
    captured = capsys.readouterr()
    assert f"{baseline_shards.SHARD_DIR}: invalid baseline" in captured.err
    assert "missing" in captured.err


def test_an_empty_committed_baseline_still_enforces_the_guard(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed baseline with zero shards (all debt paid off) still has its
    README.md tracked by git, so it is NOT indistinguishable from a revision
    that predates the shard scheme — the guard must keep comparing against it,
    not skip itself, and reject a freshly added entry."""
    _clear_baseline_dir(tmp_path)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", baseline_shards.SHARD_DIR)
    _git(tmp_path, "commit", "--quiet", "-m", "Empty baseline, fully paid off")

    directory = _clear_baseline_dir(tmp_path)
    (directory / "tests.json").write_text(
        json.dumps({"ambient_state": {"base/new.py::import-time-call:atexit.register": 2}}),
        encoding="utf-8",
    )

    (tmp_path / "base").mkdir()
    (tmp_path / "base/new.py").write_text(
        "import atexit\natexit.register(lambda: None)\natexit.register(lambda: None)\n",
        encoding="utf-8",
    )

    assert lcs.main([], repo_root=tmp_path) == 1
    captured = capsys.readouterr()
    assert "guard skipped" not in captured.err
    assert "added ambient_state entry base/new.py::import-time-call:atexit.register" in captured.out


@pytest.mark.parametrize("command", ["ls-tree", "cat-file", "show", "diff", "merge-base"])
def test_cli_fails_when_baseline_history_cannot_be_read(
    tmp_path: pathlib.Path, command: str
) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    binary = tmp_path / "bin/git"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"if {command!r} in sys.argv[1:]:\n"
        "    sys.stderr.write('injected Git read failure')\n    sys.exit(71)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    history = tmp_path / "history"
    history.mkdir()
    directory = _clear_baseline_dir(history)
    (directory / "rules.json").write_text('{"ambient_state": 1}\n', encoding="utf-8")
    (directory / "base.json").write_text('{"ambient_state": {}}\n', encoding="utf-8")
    _git(history, "init", "--quiet")
    _git(history, "add", baseline_shards.SHARD_DIR)
    _git(history, "commit", "--quiet", "-m", "Historical rule metadata")
    revision = _git(history, "rev-parse", "HEAD").stdout.strip()
    repo = pathlib.Path(lcs.__file__).resolve().parents[2]
    env = dict(
        os.environ,
        PATH=f"{binary.parent}{os.pathsep}{os.environ['PATH']}",
        GIT_ALTERNATE_OBJECT_DIRECTORIES=str(history / ".git/objects"),
        LINT_STRUCTURE_BASELINE_BASE=revision,
    )
    if command == "merge-base":
        env.pop("LINT_STRUCTURE_BASELINE_BASE")
    result = _run(
        [
            sys.executable,
            str(repo / "scripts/lint/code_structure.py"),
            str(repo / "scripts/structure/baseline_shards.py"),
        ],
        cwd=repo,
        env=env,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "guard skipped" not in result.stderr
    assert "exit status 71" in result.stdout + result.stderr


def test_actual_cli_compares_an_explicit_empty_git_tree(tmp_path: pathlib.Path) -> None:
    _git(tmp_path, "init", "--quiet")
    tree = _git(tmp_path, "mktree", input="").stdout.strip()
    for component in reversed(baseline_shards.SHARD_DIR.split("/")):
        tree = _git(tmp_path, "mktree", input=f"040000 tree {tree}\t{component}\n").stdout.strip()
    revision = _git(tmp_path, "commit-tree", tree, input="Empty baseline tree\n").stdout.strip()
    repo = pathlib.Path(lcs.__file__).resolve().parents[2]
    env = dict(
        os.environ,
        GIT_ALTERNATE_OBJECT_DIRECTORIES=str(tmp_path / ".git/objects"),
        LINT_STRUCTURE_BASELINE_BASE=revision,
    )
    result = _run(
        [
            sys.executable,
            str(repo / "scripts/lint/code_structure.py"),
            str(repo / "scripts/structure/baseline_shards.py"),
        ],
        cwd=repo,
        env=env,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "guard skipped" not in result.stderr
    assert "added ambient_state entry" in result.stdout
