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
        for path in directory.rglob("*.json"):
            path.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Structure baseline shards.\n", encoding="utf-8")
    for name, shard in baseline_shards.split(data).items():
        # Single concatenated string, not chained `/`: keeps an adversarial shard
        # name from being treated as an absolute-path override that discards
        # `directory`.
        pathlib.Path(f"{directory}/{name}.json").parent.mkdir(parents=True, exist_ok=True)
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
    monkeypatch.setenv("LINT_STRUCTURE_BASELINE_BASE", "HEAD")


def _ambient_source() -> str:
    return "import atexit\natexit.register(lambda: None)\n" + "".join(
        f"VALUE_{index} = {index}\n" for index in range(60)
    )


def _freeze_ambient(root: pathlib.Path, names: tuple[str, ...] = ("q",)) -> None:
    entries = {}
    for name in names:
        path = f"base/{name}.py"
        source = root / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_ambient_source(), encoding="utf-8")
        entries[f"{path}::import-time-call:atexit.register"] = 1
    _write_baseline(root, {"ambient_state": entries})
    _git(root, "init", "--quiet")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "Existing ambient sites")


def _move_ambient(root: pathlib.Path, names: tuple[str, ...] = ("q",)) -> None:
    entries = {}
    for name in names:
        new = f"base/{name}_moved.py"
        _git(root, "mv", f"base/{name}.py", new)
        (root / new).write_text(_ambient_source() + "# moved imports\n", encoding="utf-8")
        entries[f"{new}::import-time-call:atexit.register"] = 1
    _write_baseline(root, {"ambient_state": entries})


def test_detected_moves_carry_existing_baseline_keys_and_rewrites_do_not(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze_ambient(tmp_path)
    assert lcs.main([], repo_root=tmp_path) == 0
    _move_ambient(tmp_path)
    assert lcs.main([], repo_root=tmp_path) == 0
    assert capsys.readouterr().out == ""
    moved = tmp_path / "base/q_moved.py"
    moved.write_text("import atexit\natexit.register(lambda: None)\n", encoding="utf-8")
    assert lcs.main([], repo_root=tmp_path) == 1
    assert "added ambient_state entry base/q_moved.py" in capsys.readouterr().out


def test_package_moves_carry_keys_past_the_git_rename_limit(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze_ambient(tmp_path, ("q", "r"))
    _git(tmp_path, "config", "diff.renameLimit", "1")
    _move_ambient(tmp_path, ("q", "r"))
    assert lcs.main([], repo_root=tmp_path) == 0
    assert capsys.readouterr().out == ""


def test_a_move_whose_old_path_is_rewritten_still_carries(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze_ambient(tmp_path)
    _move_ambient(tmp_path)
    (tmp_path / "base/q.py").write_text('"""A shell."""\n', encoding="utf-8")
    _git(tmp_path, "add", "base/q.py")
    assert lcs.main([], repo_root=tmp_path) == 0
    assert capsys.readouterr().out == ""


def test_detected_move_cannot_raise_the_carried_site_count(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _freeze_ambient(tmp_path)
    _move_ambient(tmp_path)
    moved = tmp_path / "base/q_moved.py"
    moved.write_text(_ambient_source() + "atexit.register(lambda: None)\n", encoding="utf-8")
    _write_baseline(
        tmp_path, {"ambient_state": {"base/q_moved.py::import-time-call:atexit.register": 2}}
    )
    assert lcs.main([], repo_root=tmp_path) == 1
    assert "raised ambient_state entry base/q_moved.py" in capsys.readouterr().out


@pytest.mark.parametrize("cc,rc", [(14, 0), (15, 1), (16, 1)])
def test_file_and_function_renames_do_not_grant_budget_allowances(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], cc: int, rc: int
) -> None:
    _freeze(tmp_path, "tests/q.py", cc=cc)
    _git(tmp_path, "mv", "tests/q.py", "tests/q_moved.py")
    moved = tmp_path / "tests/q_moved.py"
    moved.write_text(_branches(cc).replace("def f(", "def renamed("), encoding="utf-8")
    assert lcs.main([], repo_root=tmp_path) == rc
    assert (f"tests/q_moved.py::renamed: complexity {cc}" in capsys.readouterr().out) == bool(rc)
