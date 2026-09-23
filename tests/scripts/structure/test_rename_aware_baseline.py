"""Rename-aware frozen baseline keys: detected moves carry keys, value caps stay."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from scripts import lint_code_structure as lcs

_BASELINE = "scripts/structure/baseline.json"


def _write_baseline(root: pathlib.Path, data: dict[str, dict[str, int]]) -> pathlib.Path:
    path = root / _BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _read_baseline(root: pathlib.Path) -> dict[str, dict[str, int]]:
    return json.loads((root / _BASELINE).read_text(encoding="utf-8"))


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
        {"directories": {}, "files": {}, "complexity": {f"{path}::f": cc}, "nesting": {}},
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
        {"directories": {}, "files": {"tests/big.py": 805}, "complexity": {}, "nesting": {}},
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


def test_directory_keys_are_not_remapped() -> None:
    remapped = lcs._remap_renamed_keys(
        "directories", {"tests/scripts": 60}, {"tests/scripts/a.py": "tests/scripts/b.py"}
    )
    assert remapped == {"tests/scripts": 60}
