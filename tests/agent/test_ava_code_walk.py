"""Unit tests for `plugins.ava_code._walk.find_context_files_along_path` behavior.

Boundaries: along the path, the farthest (shallowest) ancestor of {git_root, $HOME}. If neither
is on the path, returns [] (no walk). Collects AGENTS.md and CLAUDE.md, at the same level AGENTS precedes CLAUDE.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ava_builtins.plugins.ava_code._walk import (
    _git_root,
    find_context_files_along_path,
    project_skill_roots,
)


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


def test_git_root_returns_none_for_non_git_dir(tmp_path: Path):
    """`_git_root` returns None for a non-git directory."""
    assert _git_root(tmp_path) is None


def test_git_root_returns_root_for_git_dir(tmp_path: Path):
    """`_git_root` returns the toplevel inside a git repo."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src"
    sub.mkdir()

    assert _git_root(sub) == root.resolve()


def test_git_root_handles_nonexistent_start(tmp_path: Path):
    """start does not exist → None (no raise)."""
    fake = tmp_path / "does-not-exist"
    assert _git_root(fake) is None


def test_git_root_reresolves_after_git_init(tmp_path: Path):
    """Non-repo resolves to None first (no caching), after git init immediately returns the new root —
    stale None must not block a later-created repo (project-local skills / AGENTS.md depend on this)."""
    d = (tmp_path / "later-repo").resolve()
    d.mkdir()
    assert _git_root(d) is None  # not a repo yet, None not cached
    _make_git_repo(d)
    assert _git_root(d) == d  # now surfaces


def test_project_skill_roots_order_claude_agents_ava(tmp_path: Path):
    """When all three skill folders exist, the returned order is .claude, .agents, .ava —
    scanning last-wins, so .ava (Ava-native) overrides same-named compat skills."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".agents" / "skills").mkdir(parents=True)
    (root / ".ava" / "skills").mkdir(parents=True)

    roots = project_skill_roots(root)
    assert [r.parent.name for r in roots] == [".claude", ".agents", ".ava"]


def test_project_skill_roots_agents_joins_claude_ava(tmp_path: Path):
    """Each of .claude / .agents / .ava is returned only when it exists; a repo with
    just .agents/skills (the open-standard layout) is discovered like the legacy ones."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    assert project_skill_roots(root) == []

    (root / ".agents" / "skills").mkdir(parents=True)
    assert project_skill_roots(root) == [root / ".agents" / "skills"]

    (root / ".claude" / "skills").mkdir(parents=True)
    assert project_skill_roots(root) == [root / ".claude" / "skills", root / ".agents" / "skills"]


def test_project_skill_roots_follows_links_back_to_agents(tmp_path: Path):
    """The canonical repo layout — real `.agents/skills`, `.claude/skills` and
    `.ava/skills` as symlinks back to it — resolves to all three entries (is_dir
    follows links), and the runtime dedups same-content skills by hash."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / ".agents" / "skills").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".ava").mkdir()
    (root / ".claude" / "skills").symlink_to("../.agents/skills")
    (root / ".ava" / "skills").symlink_to("../.agents/skills")

    roots = project_skill_roots(root)
    assert [r.parent.name for r in roots] == [".claude", ".agents", ".ava"]
    assert all(r.is_dir() for r in roots)
    assert [r.resolve() for r in roots] == [root / ".agents" / "skills"] * 3


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
