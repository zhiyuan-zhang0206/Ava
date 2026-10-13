"""Unit tests for `base.packages.skills.project_roots`: git-root resolution and the
project-local skill folders (`.claude`, `.agents`, `.ava` under the repo root)."""

import subprocess
from pathlib import Path

from base.packages.skills.project_roots import git_root, project_skill_roots


def _make_git_repo(root: Path) -> None:
    """Create a minimum git repo under tmp_path."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_git_root_returns_none_for_non_git_dir(tmp_path: Path):
    """`git_root` returns None for a non-git directory."""
    assert git_root(tmp_path) is None


def test_git_root_returns_root_for_git_dir(tmp_path: Path):
    """`git_root` returns the toplevel inside a git repo."""
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src"
    sub.mkdir()

    assert git_root(sub) == root.resolve()


def test_git_root_handles_nonexistent_start(tmp_path: Path):
    """start does not exist → None (no raise)."""
    fake = tmp_path / "does-not-exist"
    assert git_root(fake) is None


def test_git_root_reresolves_after_git_init(tmp_path: Path):
    """Non-repo resolves to None first (no caching), after git init immediately returns the new root —
    stale None must not block a later-created repo (project-local skills / AGENTS.md depend on this)."""
    d = (tmp_path / "later-repo").resolve()
    d.mkdir()
    assert git_root(d) is None  # not a repo yet, None not cached
    _make_git_repo(d)
    assert git_root(d) == d  # now surfaces


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
