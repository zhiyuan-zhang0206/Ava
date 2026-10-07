"""Git rename detection and strict budgets after file moves."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards


def _write_baseline(root: pathlib.Path, data: dict[str, dict[str, int]]) -> pathlib.Path:
    """Write the baseline as shards under scripts/structure/baseline/, replacing any
    shard files already there, plus the directory's README.md: read_worktree()
    requires the directory to exist, and the README is what keeps git tracking it
    even with zero shards. Returns the shard directory."""
    directory = root / baseline_shards.SHARD_DIR
    if directory.is_dir():
        for path in directory.glob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    for name, shard in baseline_shards.split(data).items():
        # Single concatenated string, not chained `/`: keeps an adversarial shard
        # name from being treated as an absolute-path override that discards
        # `directory`.
        (pathlib.Path(f"{directory}/{name}.json")).write_text(
            baseline_shards.render(shard), encoding="utf-8"
        )
    return directory


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed test commands, never external input
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


def _branches(cc: int) -> str:
    return "def f(x):\n" + "    if x: pass\n" * (cc - 1) + "    return x\n"


def _freeze(tmp_path: pathlib.Path, path: str, *, cc: int = 16) -> None:
    """Commit a module whose measured complexity is `cc`."""
    source = tmp_path / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_branches(cc), encoding="utf-8")
    _write_baseline(tmp_path, {})
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "Commit a measured module")


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)


def test_rename_map_follows_detected_moves(tmp_path: pathlib.Path) -> None:
    _freeze(tmp_path, "tests/q.py")
    assert lcs._rename_map("HEAD") == {}
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    assert lcs._rename_map("HEAD") == {"tests/q.py": "tests/q_moved.py"}
    moved = tmp_path / "tests/q_moved.py"
    moved.write_text(_branches(16) + "# import path rewritten by the move\n", encoding="utf-8")
    assert lcs._rename_map("HEAD") == {"tests/q.py": "tests/q_moved.py"}
    moved.write_text("x = 1\n" * 300 + _branches(16), encoding="utf-8")
    assert lcs._rename_map("HEAD") == {}


def test_rename_map_is_not_capped_by_the_diff_rename_limit(tmp_path: pathlib.Path) -> None:
    """A package-wide move rewrites the import lines of thousands of files at once.
    Past `diff.renameLimit`, git silently skips pairing the edited moves, and every
    carried baseline key would read as a new entry — the map must not be capped."""
    _freeze(tmp_path, "tests/q.py")
    (tmp_path / "tests/r.py").write_text(_branches(16), encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "A second module")
    _git(tmp_path, "config", "diff.renameLimit", "1")
    for name in ("q", "r"):
        _git(tmp_path, "mv", f"tests/{name}.py", f"tests/{name}_moved.py")
        moved = tmp_path / f"tests/{name}_moved.py"
        moved.write_text(_branches(16) + "# import path rewritten by the move\n", encoding="utf-8")

    assert lcs._rename_map("HEAD") == {
        "tests/q.py": "tests/q_moved.py",
        "tests/r.py": "tests/r_moved.py",
    }


def test_a_move_whose_old_path_is_rewritten_still_carries(tmp_path: pathlib.Path) -> None:
    """A module that moves while a small new file takes its old path (a shell left
    behind) is a rewrite of the old path plus a move of its content."""
    _freeze(tmp_path, "tests/q.py")
    # Git only breaks a rewritten file above a minimum size; pad the frozen module.
    padding = "".join(f"VALUE_{index} = {index}\n" for index in range(60))
    (tmp_path / "tests/q.py").write_text(_branches(16) + padding, encoding="utf-8")
    _git(tmp_path, "commit", "--quiet", "-am", "Pad the measured module")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    (tmp_path / "tests/q.py").write_text('"""A shell."""\n', encoding="utf-8")
    _git(tmp_path, "add", "tests/q.py")

    assert lcs._rename_map("HEAD") == {"tests/q.py": "tests/q_moved.py"}
    assert lcs.main([]) == 1


@pytest.mark.parametrize("cc,rc", [(14, 0), (15, 1), (16, 1)])
def test_file_and_function_renames_do_not_grant_budget_allowances(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], cc: int, rc: int
) -> None:
    _freeze(tmp_path, "tests/q.py", cc=cc)
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    moved = tmp_path / "tests/q_moved.py"
    moved.write_text(_branches(cc).replace("def f(", "def renamed("), encoding="utf-8")
    assert lcs.main([]) == rc
    assert (f"tests/q_moved.py::renamed: complexity {cc}" in capsys.readouterr().out) == bool(rc)
