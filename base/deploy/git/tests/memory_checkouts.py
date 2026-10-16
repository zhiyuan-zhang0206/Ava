from pathlib import Path

import pytest

from base.deploy.git.memory_repo import init_local_repo, run_git


def commit_note(root: Path, body: str) -> str:
    (root / "note.md").write_text(f"---\ntype: Memory\ntitle: Note\n---\n\n{body}\n")
    run_git("add", "note.md", cwd=root)
    run_git("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-qm", body, cwd=root)
    return run_git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def memory_checkout(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    root = tmp_path / "checkout"
    remote = tmp_path / "remote.git"
    init_local_repo("main", source)
    commit_note(source, "before refresh")
    run_git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    run_git("remote", "add", "origin", str(remote), cwd=source)
    run_git("clone", str(remote), str(root), cwd=tmp_path)
    return source, root
