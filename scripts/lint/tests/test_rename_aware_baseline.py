"""Rename-aware frozen baseline keys: detected moves carry keys, value caps stay."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from scripts.lint import code_structure as lcs
from scripts.structure import baseline_shards

_SECTIONS = ("directories", "files", "complexity", "nesting", *lcs._SITE_SECTIONS)


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


def _read_baseline(root: pathlib.Path) -> dict[str, dict[str, int]]:
    return baseline_shards.merge(baseline_shards.read_worktree(root), _SECTIONS)


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
    """Commit a repo whose frozen baseline names one complexity key at `cc`."""
    source = tmp_path / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_branches(cc), encoding="utf-8")
    _write_baseline(
        tmp_path,
        {
            "directories": {},
            "files": {},
            "complexity": {f"{path}::f": cc},
            "nesting": {},
            "private_imports": {},
            "owner_bypasses": {},
            "path_imports": {},
        },
    )
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "Freeze one complexity key")


def _migrate(tmp_path: pathlib.Path, old: str, new: str, *, cc: int = 16) -> None:
    data = _read_baseline(tmp_path)
    data["complexity"] = {f"{new}::f": cc}
    _write_baseline(tmp_path, data)


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lcs, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("LINT_STRUCTURE_BASELINE_BASE", raising=False)


def test_move_with_migrated_baseline_is_green(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(tmp_path, "tests/q.py")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    _migrate(tmp_path, "tests/q.py", "tests/q_moved.py")

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_move_with_explicit_base_revision_is_green(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tmp_path, "tests/q.py")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    _migrate(tmp_path, "tests/q.py", "tests/q_moved.py")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "Move with migrated baseline")
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD~1")

    assert lcs.main([]) == 0


def test_move_with_import_touch_up_inherits(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(tmp_path, "tests/q.py")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    moved = tmp_path / "tests/q_moved.py"
    moved.write_text(_branches(16) + "# import path rewritten by the move\n", encoding="utf-8")
    _migrate(tmp_path, "tests/q.py", "tests/q_moved.py")

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_move_with_unmigrated_baseline_teaches_migration(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(tmp_path, "tests/q.py")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")

    assert lcs.main([]) == 1
    captured = capsys.readouterr()
    assert "tests/q.py::f was not migrated after its file moved to tests/q_moved.py" in captured.out
    assert "migrate the baseline entry tests/q.py::f" in captured.out


@pytest.mark.parametrize("migrated_value", [17, 16])
def test_move_with_raised_value_is_rejected(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], migrated_value: int
) -> None:
    _freeze(tmp_path, "tests/q.py")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    moved = tmp_path / "tests/q_moved.py"
    moved.write_text(_branches(17) + "# moved and changed\n", encoding="utf-8")
    _migrate(tmp_path, "tests/q.py", "tests/q_moved.py", cc=migrated_value)

    assert lcs.main([]) == 1
    captured = capsys.readouterr()
    if migrated_value == 17:
        assert "raised complexity entry tests/q_moved.py::f from 16 to 17" in captured.out
    else:
        assert "grew above its frozen baseline value (16)" in captured.out


def test_move_with_refactor_below_threshold_is_green(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze(tmp_path, "tests/q.py")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    (tmp_path / "tests/q_moved.py").write_text(_branches(12), encoding="utf-8")
    data = _read_baseline(tmp_path)
    data["complexity"] = {}
    _write_baseline(tmp_path, data)

    assert lcs.main([]) == 0
    assert capsys.readouterr().out == ""


def test_files_budget_move_inherits(tmp_path: pathlib.Path) -> None:
    big = tmp_path / "tests/big.py"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("x = 1\n" * 805, encoding="utf-8")
    _write_baseline(
        tmp_path,
        {
            "directories": {},
            "files": {"tests/big.py": 805},
            "complexity": {},
            "nesting": {},
            "private_imports": {},
            "owner_bypasses": {},
            "path_imports": {},
        },
    )
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "Freeze oversized file")
    _git(tmp_path, "mv", "tests/big.py", "tests/big_moved.py")
    data = _read_baseline(tmp_path)
    data["files"] = {"tests/big_moved.py": 805}
    _write_baseline(tmp_path, data)

    assert lcs.main([]) == 0


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
    _git(tmp_path, "commit", "--quiet", "-am", "Grow the frozen module")
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    (tmp_path / "tests/q.py").write_text('"""A shell."""\n', encoding="utf-8")
    _git(tmp_path, "add", "tests/q.py")
    _migrate(tmp_path, "tests/q.py", "tests/q_moved.py")

    assert lcs._rename_map("HEAD") == {"tests/q.py": "tests/q_moved.py"}
    assert lcs.main([]) == 0


def test_directory_keys_stay_when_files_move_within_them() -> None:
    remapped = lcs._remap_renamed_keys(
        "directories", {"tests/scripts": 60}, {"tests/scripts/a.py": "tests/scripts/b.py"}
    )
    assert remapped == {"tests/scripts": 60}


def test_directory_keys_follow_a_wholesale_directory_move(tmp_path: pathlib.Path) -> None:
    renames = {
        "tests/old/a.py": "tests/new/a.py",
        "tests/old/sub/b.py": "tests/new/sub/b.py",
        "tests/split/c.py": "tests/elsewhere/c.py",
        "tests/split/d.py": "tests/other/d.py",
    }
    remapped = lcs._remap_renamed_keys(
        "directories", {"tests/old": 25, "tests/old/sub": 22, "tests/split": 21}, renames
    )
    # A directory whose files disagree on the target is not carried.
    assert remapped == {"tests/new": 25, "tests/new/sub": 22, "tests/split": 21}

    (tmp_path / "tests/old").mkdir(parents=True)
    # A directory that still exists was split, not moved.
    assert lcs._remap_renamed_keys("directories", {"tests/old": 25}, renames) == {"tests/old": 25}


def _freeze_package(tmp_path: pathlib.Path) -> None:
    """A governed top-level package with a frozen complexity key and directory key."""
    for index in range(21):
        path = tmp_path / f"oldpkg/sub/m{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE_{index} = {index}\n" * 20, encoding="utf-8")
    (tmp_path / "oldpkg/q.py").write_text(_branches(16), encoding="utf-8")
    data: dict[str, dict[str, int]] = {kind: {} for kind in _SECTIONS}
    data["complexity"] = {"oldpkg/q.py::f": 16}
    data["directories"] = {"oldpkg/sub": 21}
    _write_baseline(tmp_path, data)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "Freeze a governed package")


@pytest.mark.parametrize("directory_value,rc", [(21, 0), (22, 1)])
def test_a_renamed_top_level_package_carries_its_frozen_keys(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    directory_value: int,
    rc: int,
) -> None:
    """The base revision's baseline still names the old package, which the current
    scope no longer governs: it validates through the rename, and the moved
    directory's key carries (a raise stays a violation)."""
    _freeze_package(tmp_path)
    _git(tmp_path, "mv", "oldpkg", "newpkg")
    data = _read_baseline(tmp_path)
    data["complexity"] = {"newpkg/q.py::f": 16}
    data["directories"] = {"newpkg/sub": directory_value}
    _write_baseline(tmp_path, data)
    monkeypatch.setattr(lcs, "_SCAN_DIRS", ("newpkg",))
    monkeypatch.setattr(lcs, "_STRUCTURE_DIRS", ("newpkg", "tests", "scripts"))

    assert lcs.main([]) == rc
    captured = capsys.readouterr()
    assert "invalid base baseline" not in captured.out
    if rc:
        assert "raised directories entry newpkg/sub from 21 to 22" in captured.out
    else:
        assert captured.out == ""
