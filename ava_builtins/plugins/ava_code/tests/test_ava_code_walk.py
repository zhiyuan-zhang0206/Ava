import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ava_builtins.plugins.ava_code._walk import find_context_files_along_path


def _make_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_walk_in_git_repo_collects_along_path(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src" / "module"
    sub.mkdir(parents=True)

    (root / "AGENTS.md").write_text("root\n")
    (sub / "AGENTS.md").write_text("module\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    assert [str(p.parent) for p in result] == [str(root), str(sub)]


def test_walk_collects_claude_md_too(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src"
    sub.mkdir()

    (root / "CLAUDE.md").write_text("root claude\n")
    (sub / "CLAUDE.md").write_text("sub claude\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    assert [str(p) for p in result] == [
        str(root / "CLAUDE.md"),
        str(sub / "CLAUDE.md"),
    ]


def test_walk_same_dir_agents_before_claude(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "AGENTS.md").write_text("agents\n")
    (root / "CLAUDE.md").write_text("claude\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(root)

    assert [p.name for p in result] == ["AGENTS.md", "CLAUDE.md"]


def test_walk_mixed_files_across_levels(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src"
    sub.mkdir()

    (root / "AGENTS.md").write_text("root agents\n")
    (sub / "AGENTS.md").write_text("sub agents\n")
    (sub / "CLAUDE.md").write_text("sub claude\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    assert [str(p) for p in result] == [
        str(root / "AGENTS.md"),
        str(sub / "AGENTS.md"),
        str(sub / "CLAUDE.md"),
    ]


def test_walk_outside_git_and_home_returns_empty(tmp_path: Path):
    target = tmp_path / "isolated" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    with patch.dict(os.environ, {"HOME": str(fake_home)}):
        result = find_context_files_along_path(target)

    assert result == []


def test_walk_in_home_only_no_git(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    sub = fake_home / "work"
    sub.mkdir()
    (fake_home / "AGENTS.md").write_text("home\n")
    (sub / "AGENTS.md").write_text("work\n")

    with patch.dict(os.environ, {"HOME": str(fake_home)}):
        result = find_context_files_along_path(sub / "foo.py")

    paths = [str(p.parent) for p in result]
    assert paths == [str(fake_home), str(sub)]


def test_walk_skips_missing_agents_md(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "a" / "b" / "c"
    sub.mkdir(parents=True)

    (root / "AGENTS.md").write_text("root\n")
    (sub / "AGENTS.md").write_text("c\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    paths = [str(p.parent) for p in result]
    assert paths == [str(root), str(sub)]


def test_walk_target_is_directory(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "AGENTS.md").write_text("root\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(root)

    assert [str(p.parent) for p in result] == [str(root)]


def test_walk_nonexistent_target_returns_empty(tmp_path: Path):
    fake = tmp_path / "does-not-exist" / "foo.py"
    result = find_context_files_along_path(fake)
    assert result == []


def test_walk_agents_md_is_directory_not_file_skipped(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "AGENTS.md").mkdir()
    sub = root / "x"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("legit\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    assert len(result) == 1
    assert result[0].parent == sub


@pytest.mark.skipif(
    not subprocess.run(["which", "git"], capture_output=True, check=False).stdout,
    reason="git not in PATH",
)
def test_walk_with_home_above_git_root(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    repo = fake_home / "proj"
    repo.mkdir()
    _make_git_repo(repo)
    sub = repo / "src"
    sub.mkdir()

    (fake_home / "AGENTS.md").write_text("global\n")
    (repo / "AGENTS.md").write_text("project\n")
    (sub / "AGENTS.md").write_text("module\n")

    with patch.dict(os.environ, {"HOME": str(fake_home)}):
        result = find_context_files_along_path(sub / "foo.py")

    paths = [str(p.parent) for p in result]
    assert paths == [str(fake_home), str(repo), str(sub)]
