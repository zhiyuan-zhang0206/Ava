import subprocess
from pathlib import Path

from base.packages.skills.project_roots import git_root, project_skill_roots


def _make_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_git_root_returns_none_for_non_git_dir(tmp_path: Path):
    assert git_root(tmp_path) is None


def test_git_root_returns_root_for_git_dir(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    sub = root / "src"
    sub.mkdir()

    assert git_root(sub) == root.resolve()


def test_git_root_handles_nonexistent_start(tmp_path: Path):
    fake = tmp_path / "does-not-exist"
    assert git_root(fake) is None


def test_git_root_reresolves_after_git_init(tmp_path: Path):
    d = (tmp_path / "later-repo").resolve()
    d.mkdir()
    assert git_root(d) is None
    _make_git_repo(d)
    assert git_root(d) == d


def test_project_skill_roots_order_claude_agents_meiva(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".agents" / "skills").mkdir(parents=True)
    (root / ".meiva" / "skills").mkdir(parents=True)

    roots = project_skill_roots(root)
    assert [r.parent.name for r in roots] == [".claude", ".agents", ".meiva"]


def test_project_skill_roots_agents_joins_claude_meiva(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    assert project_skill_roots(root) == []

    (root / ".agents" / "skills").mkdir(parents=True)
    assert project_skill_roots(root) == [root / ".agents" / "skills"]

    (root / ".claude" / "skills").mkdir(parents=True)
    assert project_skill_roots(root) == [root / ".claude" / "skills", root / ".agents" / "skills"]


def test_project_skill_roots_follows_links_back_to_agents(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / ".agents" / "skills").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".meiva").mkdir()
    (root / ".claude" / "skills").symlink_to("../.agents/skills")
    (root / ".meiva" / "skills").symlink_to("../.agents/skills")

    roots = project_skill_roots(root)
    assert [r.parent.name for r in roots] == [".claude", ".agents", ".meiva"]
    assert all(r.is_dir() for r in roots)
    assert [r.resolve() for r in roots] == [root / ".agents" / "skills"] * 3
