"""`scripts/setup-worktree.sh <task>` creates a worktree and bootstraps it.

Process-level checks on a throwaway repository with its own `origin`. The real
script and its two Python helpers are copied in; `uv` and `npm` are PATH stubs
that record their calls (and the directory they ran in), so nothing is installed.
"""

# ruff: noqa: S603 — subprocess commands use only test-owned paths and fixture literals.

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SEEDED = (
    "scripts/setup-worktree.sh",
    "scripts/provision/check_git_hooks.py",
    "scripts/host_ops/guard_editable_venv.py",
)
_UV = """\
#!/bin/sh
echo "uv $* @ $PWD" >> "$SETUP_CALLS"
[ -z "${FAIL_UV:-}" ] || { echo "uv: simulated failure" >&2; exit 1; }
if [ "$1" = sync ]; then
  mkdir -p .venv/bin
  printf '#!/bin/sh\\necho "python $* @ $PWD" >> "$SETUP_CALLS"\\n' > .venv/bin/python
  chmod +x .venv/bin/python
  [ -z "${REWRITE_LOCK:-}" ] || echo rewritten >> uv.lock
fi
"""
_NPM = """\
#!/bin/sh
echo "npm $* @ $PWD" >> "$SETUP_CALLS"
mkdir -p node_modules
"""


def _stub_dir(parent: Path, name: str, **scripts: str) -> Path:
    directory = parent / name
    directory.mkdir(parents=True)
    for tool, body in scripts.items():
        stub = directory / tool
        stub.write_text(body)
        stub.chmod(0o755)
    return directory


class _World:
    """A main clone with an `origin`, the stubs, and a call log."""

    def __init__(self, root: Path, *, hooks: bool = True) -> None:
        self.root = root
        self.calls_file = root / "calls.log"
        self.stubs = _stub_dir(root, "stubs", uv=_UV, npm=_NPM)
        self.origin = root / "origin.git"
        self.main = root / "main-clone"
        self.git(root, "init", "-q", "--bare", "--initial-branch=main", str(self.origin))
        self.git(root, "init", "-q", "--initial-branch=main", str(self.main))
        self._identify(self.main)
        for rel in _SEEDED:
            target = self.main / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_REPO / rel, target)
        (self.main / "cli").mkdir()
        (self.main / "cli/python_install.py").write_text("# stub: the python stub only logs\n")
        (self.main / "ui/web").mkdir(parents=True)
        (self.main / "ui/web/package-lock.json").write_text("{}\n")
        (self.main / "uv.lock").write_text("version = 1\n")
        (self.main / ".gitignore").write_text(".venv/\nnode_modules/\n.worktrees/\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "seed")
        self.git(self.main, "remote", "add", "origin", str(self.origin))
        self.git(self.main, "push", "-q", "origin", "main")
        self.git(self.main, "fetch", "-q", "origin")
        if hooks:
            self._install_hooks()

    def _identify(self, repo: Path) -> None:
        self.git(repo, "config", "user.name", "test")
        self.git(repo, "config", "user.email", "test@ava")

    def _install_hooks(self) -> None:
        interpreter = self.main / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        for hook_type in ("pre-commit", "pre-push"):
            hook = self.main / ".git/hooks" / hook_type
            hook.write_text(
                "#!/usr/bin/env bash\n"
                "# ID: 138fd403232d2ddd5efb44317e38bf03\n"
                f"INSTALL_PYTHON='{interpreter}'\n"
                f"ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type={hook_type})\n"
            )
            hook.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "UV_")) and key != "VIRTUAL_ENV"
        }
        env.update(
            PATH=f"{self.stubs}{os.pathsep}{os.environ['PATH']}",
            HOME=str(self.root / "home"),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            SETUP_CALLS=str(self.calls_file),
        )
        env.update(extra)
        return env

    def git(self, cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env(),
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def setup(
        self, *args: str, cwd: Path | None = None, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the script a checkout carries, from that checkout (default: the main clone)."""
        cwd = cwd or self.main
        return subprocess.run(
            ["bash", "scripts/setup-worktree.sh", *args],
            cwd=cwd,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def advance_origin(self) -> str:
        """Land a commit on origin/main that the main clone has not fetched; return its sha."""
        other = self.root / "other-clone"
        self.git(self.root, "clone", "-q", str(self.origin), str(other))
        self._identify(other)
        (other / "landed.txt").write_text("landed\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "landed after the clone")
        self.git(other, "push", "-q", "origin", "main")
        return self.git(other, "rev-parse", "HEAD")

    def calls(self) -> list[tuple[str, Path]]:
        if not self.calls_file.exists():
            return []
        pairs = [line.rpartition(" @ ") for line in self.calls_file.read_text().splitlines()]
        return [(command, Path(cwd).resolve()) for command, _, cwd in pairs]

    def worktree(self, task: str) -> Path:
        return (self.main / ".worktrees" / task).resolve()

    def branch_exists(self, name: str) -> bool:
        done = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=self.main,
            env=self.env(),
            check=False,
        )
        return done.returncode == 0


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def _ready_line(path: Path, branch: str) -> str:
    return f"worktree ready: {path} (branch {branch})"


def test_creates_the_worktree_off_fresh_origin_main_and_bootstraps_in_order(
    world: _World,
) -> None:
    landed = world.advance_origin()
    stale_main = world.git(world.main, "rev-parse", "main")

    done = world.setup("t1")

    path = world.worktree("t1")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == _ready_line(path, "ava-t1")
    assert world.git(path, "rev-parse", "HEAD") == landed != stale_main
    assert world.git(path, "branch", "--show-current") == "ava-t1"
    # Branching off origin/main must not make a later bare `git push` resolve to main.
    tracking = subprocess.run(
        ["git", "config", "--get", "branch.ava-t1.merge"],
        cwd=path,
        env=world.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracking.returncode == 1
    assert world.calls() == [
        ("uv sync --frozen", path),
        (f"python {path}/cli/python_install.py --locked --inexact", path),
        ("npm ci --no-audit --no-fund", path / "ui/web"),
    ]
    assert (path / ".venv").is_dir() and not (path / ".venv").is_symlink()
    assert world.git(path, "status", "--porcelain") == ""


def test_from_inside_another_worktree_the_new_one_lands_under_the_main_clone(
    world: _World,
) -> None:
    assert world.setup("t1").returncode == 0

    done = world.setup("t2", cwd=world.worktree("t1"))

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == _ready_line(world.worktree("t2"), "ava-t2")
    assert not (world.worktree("t1") / ".worktrees").exists()


def test_rerunning_only_rebootstraps_the_existing_worktree(world: _World) -> None:
    first = world.setup("t1")
    path = world.worktree("t1")
    head = world.git(path, "rev-parse", "HEAD")

    again = world.setup("t1")

    assert (first.returncode, again.returncode) == (0, 0), again.stderr
    assert "already exists" in again.stdout
    assert again.stdout.splitlines()[-1] == _ready_line(path, "ava-t1")
    assert world.git(path, "rev-parse", "HEAD") == head
    assert world.git(world.main, "worktree", "list").count("\n") == 1  # main + t1 only
    commands = [command.split()[0] for command, _ in world.calls()]
    assert commands == ["uv", "python", "npm", "python", "npm"]  # the venv is built once


def test_an_existing_worktree_on_another_branch_is_refused_when_one_is_named(
    world: _World,
) -> None:
    assert world.setup("t1").returncode == 0

    done = world.setup("t1", "--branch", "other")

    assert done.returncode != 0
    assert "not 'other'" in done.stderr


def test_a_path_that_is_not_a_worktree_is_never_overwritten(world: _World) -> None:
    squatter = world.main / ".worktrees/t1"
    squatter.mkdir(parents=True)
    (squatter / "keep.txt").write_text("mine\n")

    done = world.setup("t1")

    assert done.returncode != 0
    assert "not a worktree" in done.stderr
    assert (squatter / "keep.txt").read_text() == "mine\n"
    assert not world.branch_exists("ava-t1")
    assert world.calls() == []


def test_a_registered_worktree_whose_directory_is_gone_points_at_prune(world: _World) -> None:
    assert world.setup("t1").returncode == 0
    shutil.rmtree(world.worktree("t1"))

    done = world.setup("t1")

    assert done.returncode != 0
    assert "git worktree prune" in done.stderr


def test_a_branch_checked_out_elsewhere_is_named_in_the_error(world: _World) -> None:
    assert world.setup("t1").returncode == 0

    done = world.setup("t2", "--branch", "ava-t1")

    assert done.returncode != 0
    assert f"checked out at {world.worktree('t1')}" in done.stderr
    assert not world.worktree("t2").exists()


def test_an_existing_unused_branch_is_refused_not_reused(world: _World) -> None:
    world.git(world.main, "branch", "ava-t3", "origin/main")

    done = world.setup("t3")

    assert done.returncode != 0
    assert "already exists" in done.stderr and "git branch -d ava-t3" in done.stderr
    assert not world.worktree("t3").exists()


def test_the_main_clone_is_never_bootstrapped_without_a_task(world: _World) -> None:
    done = world.setup()

    assert done.returncode != 0
    assert "main clone" in done.stderr and "<task>" in done.stderr
    assert world.calls() == []


def test_without_a_task_a_foreign_worktree_is_completed_in_place(world: _World) -> None:
    """Claude Code's own worktree tool makes one without dependencies; this finishes it."""
    native = world.main / ".claude/worktrees/native"
    world.git(world.main, "worktree", "add", "-q", "--no-track", "-b", "native", str(native))

    done = world.setup(cwd=native)

    path = native.resolve()
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == _ready_line(path, "native")
    assert [cwd for _, cwd in world.calls()] == [path, path, path / "ui/web"]


def test_a_failed_bootstrap_keeps_the_worktree_and_resumes_on_rerun(world: _World) -> None:
    failed = world.setup("t1", FAIL_UV="1")

    path = world.worktree("t1")
    assert failed.returncode != 0
    assert path.is_dir()
    assert "worktree ready" not in failed.stdout
    assert "scripts/setup-worktree.sh" in failed.stderr and "inside it" in failed.stderr

    resumed = world.setup(cwd=path)

    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout.splitlines()[-1] == _ready_line(path, "ava-t1")


def test_unhealthy_shared_hooks_stop_the_bootstrap_before_any_install(tmp_path: Path) -> None:
    world = _World(tmp_path, hooks=False)

    done = world.setup("t1")

    assert done.returncode != 0
    assert "missing or non-executable" in done.stdout  # the hook check's own report
    assert world.worktree("t1").is_dir()
    assert world.calls() == []


def test_a_rewritten_lock_file_fails_the_final_check(world: _World) -> None:
    done = world.setup("t1", REWRITE_LOCK="1")

    assert done.returncode != 0
    assert "changed tracked files" in done.stderr
    assert "worktree ready" not in done.stdout


def test_a_symlinked_venv_is_refused_on_rerun(world: _World, tmp_path: Path) -> None:
    assert world.setup("t1").returncode == 0
    path = world.worktree("t1")
    shutil.rmtree(path / ".venv")
    (path / ".venv").symlink_to(tmp_path)

    done = world.setup(cwd=path)

    assert done.returncode != 0
    assert "symlink" in done.stderr


def test_a_leaked_virtual_env_does_not_steer_the_bootstrap(world: _World, tmp_path: Path) -> None:
    done = world.setup("t1", VIRTUAL_ENV=str(tmp_path / "somebody-elses-venv"))

    assert done.returncode == 0, done.stderr


def test_branch_naming_base_override_and_task_validation(world: _World) -> None:
    sha = world.git(world.main, "rev-parse", "origin/main")

    agent_style = world.setup("ava-77-demo", "--base", sha)
    assert agent_style.returncode == 0, agent_style.stderr
    assert world.branch_exists("ava-77-demo") and not world.branch_exists("ava-ava-77-demo")

    custom = world.setup("t5", "--branch", "feature/five")
    assert custom.returncode == 0, custom.stderr
    assert custom.stdout.splitlines()[-1] == _ready_line(world.worktree("t5"), "feature/five")

    for bad in ("a/b", ".hidden", "has space"):
        refused = world.setup(bad)
        assert refused.returncode != 0 and "plain directory name" in refused.stderr
    assert world.setup("--branch", "x").returncode != 0  # a flag with no task


def test_a_missing_tool_is_reported_before_anything_is_created(tmp_path: Path) -> None:
    world = _World(tmp_path)
    without_uv = _stub_dir(tmp_path, "no-uv", npm=_NPM)
    path = f"{without_uv}{os.pathsep}/usr/bin{os.pathsep}/bin"
    if shutil.which("uv", path=path):
        pytest.skip("a system uv is installed in /usr/bin")

    done = world.setup("t1", PATH=path)

    assert done.returncode != 0
    assert "uv is not on PATH" in done.stderr
    assert not world.worktree("t1").exists()


def test_help_prints_the_usage_header(world: _World) -> None:
    done = world.setup("--help")

    assert done.returncode == 0
    assert done.stdout.startswith("The one way to get a development worktree")
    assert "bash scripts/setup-worktree.sh <task>" in done.stdout
