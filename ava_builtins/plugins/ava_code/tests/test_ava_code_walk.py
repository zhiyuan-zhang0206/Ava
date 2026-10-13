"""Unit tests for `plugins.ava_code._walk.find_context_files_along_path` behavior.

Boundaries: along the path, the farthest (shallowest) ancestor of {git_root, $HOME}. If neither
is on the path, returns [] (no walk). Collects AGENTS.md and CLAUDE.md, at the same level AGENTS precedes CLAUDE.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ava_builtins.plugins.ava_code._walk import find_context_files_along_path


def _make_git_repo(root: Path) -> None:
    """Create a minimum git repo under tmp_path."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_walk_in_git_repo_collects_along_path(tmp_path: Path):
    """target inside a git repo, walk along path up to git root, collecting all AGENTS.md along the way."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src" / "module"
    sub.mkdir(parents=True)

    (root / "AGENTS.md").write_text("root\n")
    (sub / "AGENTS.md").write_text("module\n")

    # Use monkeypatched $HOME to prevent real home interference
    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    # Order: farthest (root) → nearest (sub)
    assert [str(p.parent) for p in result] == [str(root), str(sub)]


def test_walk_collects_claude_md_too(tmp_path: Path):
    """CLAUDE.md is collected along the path just like AGENTS.md (Claude Code native context file)."""
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
    """At the same level with both AGENTS.md and CLAUDE.md → AGENTS first, CLAUDE second."""
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
    """Mixed across levels: far level AGENTS, near level CLAUDE → order is still farthest→nearest,
    within each level AGENTS before CLAUDE."""
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
    """target is neither in a git repo nor under $HOME → no walk."""
    target = tmp_path / "isolated" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    # fake $HOME does not contain target
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    with patch.dict(os.environ, {"HOME": str(fake_home)}):
        result = find_context_files_along_path(target)

    assert result == []


def test_walk_in_home_only_no_git(tmp_path: Path):
    """target is under $HOME but not in a git repo, walk up to $HOME boundary."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    sub = fake_home / "work"
    sub.mkdir()
    (fake_home / "AGENTS.md").write_text("home\n")
    (sub / "AGENTS.md").write_text("work\n")

    with patch.dict(os.environ, {"HOME": str(fake_home)}):
        result = find_context_files_along_path(sub / "foo.py")

    paths = [str(p.parent) for p in result]
    # Order: home first (farthest), work last
    assert paths == [str(fake_home), str(sub)]


def test_walk_skips_missing_agents_md(tmp_path: Path):
    """Levels without AGENTS.md are skipped, not stopping; continue climbing up."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "a" / "b" / "c"
    sub.mkdir(parents=True)

    # Only root and c have AGENTS.md, a/b in between don't
    (root / "AGENTS.md").write_text("root\n")
    (sub / "AGENTS.md").write_text("c\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    paths = [str(p.parent) for p in result]
    assert paths == [str(root), str(sub)]


def test_walk_target_is_directory(tmp_path: Path):
    """target is a directory (not file), walk starts from target itself."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "AGENTS.md").write_text("root\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(root)

    assert [str(p.parent) for p in result] == [str(root)]


def test_walk_nonexistent_target_returns_empty(tmp_path: Path):
    """target path does not exist → no walk (start missing returns directly)."""
    fake = tmp_path / "does-not-exist" / "foo.py"
    result = find_context_files_along_path(fake)
    assert result == []


def test_walk_agents_md_is_directory_not_file_skipped(tmp_path: Path):
    """AGENTS.md is a directory (rare but possible) → skip, not treated as a file."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "AGENTS.md").mkdir()  # deliberate directory
    sub = root / "x"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("legit\n")

    with patch.dict(os.environ, {"HOME": str(tmp_path / "fake-home")}):
        Path(tmp_path / "fake-home").mkdir()
        result = find_context_files_along_path(sub / "foo.py")

    # Only collect sub/AGENTS.md (file), root/AGENTS.md is a dir, skipped
    assert len(result) == 1
    assert result[0].parent == sub


@pytest.mark.skipif(
    not subprocess.run(["which", "git"], capture_output=True, check=False).stdout,
    reason="git not in PATH",
)
def test_walk_with_home_above_git_root(tmp_path: Path):
    """target is both in a git repo and under $HOME, walk to the farther boundary ($HOME is shallower
    than git_root) — should collect all AGENTS.md between $HOME and git_root."""
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
    # Farthest (home) → repo → nearest (sub)
    assert paths == [str(fake_home), str(repo), str(sub)]
