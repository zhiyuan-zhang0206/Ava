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
import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards


def _clear_baseline_dir(root: pathlib.Path) -> pathlib.Path:
    """The shard directory, emptied of any shard files already there (created if
    absent) and carrying its README.md."""
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.glob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    return directory


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed test commands, never external input.
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
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every main() call scans only its own temporary root."""
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)


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
    assert lcs.main([]) == 1
    assert f"{baseline_shards.SHARD_DIR}: invalid baseline" in capsys.readouterr().err


def test_duplicate_baseline_entry_across_shards_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _clear_baseline_dir(tmp_path)
    (directory / "base.json").write_text(
        json.dumps({"patch_targets": {"base/q.py::base.db._pool": 2}}), encoding="utf-8"
    )
    (directory / "tests.json").write_text(
        json.dumps({"patch_targets": {"base/q.py::base.db._pool": 2}}), encoding="utf-8"
    )
    assert lcs.main([]) == 1
    captured = capsys.readouterr()
    assert f"{baseline_shards.SHARD_DIR}: invalid baseline" in captured.err
    assert "duplicates patch_targets entry 'base/q.py::base.db._pool'" in captured.err


def test_missing_baseline_directory_is_an_actionable_error(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No shard directory (not even its README.md) can only be an accidental
    deletion — read_worktree() fails fast rather than silently reading it as an
    empty baseline."""
    assert lcs.main([]) == 1
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
        json.dumps({"patch_targets": {"base/new.py::base.db._pool": 2}}), encoding="utf-8"
    )

    assert lcs.main([]) == 1
    captured = capsys.readouterr()
    assert "guard skipped" not in captured.err
    assert "added patch_targets entry base/new.py::base.db._pool" in captured.out
