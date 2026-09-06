"""Explicit plugin scaffolding and its isolation from host convergence."""

import inspect
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from cli.commands import _converge, memory
from cli.commands._converge_plugins import ScaffoldResult, run_plugin_scaffolds
from shared import memory_repo, paths, proc
from shared.config import settings
from shared.machine import set_identity
from shared.plugins_config import write_local


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


def test_scaffold_runs_despite_dangling_config() -> None:
    pdir = paths.plugins_dir() / "real"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (pdir / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold():\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('1')\n",
        encoding="utf-8",
    )
    write_local({"plugins": {"real": {"enabled": True}, "vanished": {"enabled": True}}})

    result = run_plugin_scaffolds()  # must not raise

    assert result.ran == ["real"]
    assert (pdir / "scaffolded.marker").exists()


def test_converge_steps_do_not_scaffold_plugins() -> None:
    """Changing this back would reintroduce memory-repository Git work to start."""
    assert "plugin scaffolds" not in {step.name for step in _converge.CONVERGE_STEPS}

    references = {
        step.name: inspect.getsource(step.apply)
        for step in _converge.CONVERGE_STEPS
        if "run_plugin_scaffolds" in inspect.getsource(step.apply)
        or "scaffold" in inspect.getsource(step.apply)
    }
    assert references == {}


_HOST_INTEGRATION_STEP_NAMES = frozenset(
    {
        "port conflict preflight",
        "health preflight",
        "otel collector sidecar",
        "lgtm native backends",
        "lgtm observability stack",
        "permissions helper build + sign + load",
        "cross-machine transfer backend",
        "github PR capability",
        "macOS firewall allow list",
        "Homebrew formula pins",
        "reap legacy-named sessions",
        "screen capture availability",
        "accessibility availability",
        "reap stale Windows tasks",
        "health probe cron job",
        "watchdog probe job",
    }
)


def _skip_host_integration(_ctx: _converge.ConvergeCtx) -> None:
    return


def _is_not_default_home(_home: Path) -> bool:
    return False


@contextmanager
def _stub_host_integrations() -> Generator[None]:
    """Stub only production steps that reach host services.

    The test still runs the real `CONVERGE_STEPS` tuple and every file-only and
    plugin-related production step. The
    listed steps can probe, install, or register host-wide infrastructure, so
    they are no-ops here to keep the regression test hermetic.
    """
    originals: list[tuple[_converge.ConvergeStep, object]] = []
    try:
        for step in _converge.CONVERGE_STEPS:
            if step.name in _HOST_INTEGRATION_STEP_NAMES:
                originals.append((step, step.apply))
                object.__setattr__(step, "apply", _skip_host_integration)
        yield
    finally:
        for step, apply in originals:
            object.__setattr__(step, "apply", apply)


def _install_memory_plugin() -> Path:
    source = Path(__file__).parents[2] / "ava_builtins" / "plugins" / "ava_memory"
    target = paths.repo_plugins_dir() / "ava_memory"
    shutil.copytree(source, target)
    write_local({"plugins": {"ava_memory": {"enabled": True}}})
    return target


def _make_dirty_memory_repo(pool: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(pool)], check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "config", "user.email", "ava@test.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(pool), "config", "user.name", "Ava Test"], check=True)  # noqa: S603
    tracked = pool / "tracked.md"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(pool), "add", "tracked.md"], check=True)  # noqa: S603
    subprocess.run(["git", "-C", str(pool), "commit", "-qm", "seed"], check=True)  # noqa: S603
    tracked.write_text("dirty\n", encoding="utf-8")
    (pool / "untracked.md").write_text("untracked\n", encoding="utf-8")


def test_converge_ignores_a_dirty_wrong_branch_memory_pool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Converge must not invoke Git in runtime memory paths, even when poisoned."""
    _install_memory_plugin()
    set_identity(role="agent-runner", name="test-runner")
    memory_pool = paths.memory_dir()
    _make_dirty_memory_repo(memory_pool, "main")

    git_cwds: list[Path] = []
    real_run_bounded = memory_repo.run_bounded
    real_subprocess_run = subprocess.run

    def _record_run_bounded(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            git_cwds.append(Path(str(kwargs["cwd"])).resolve())
        return real_run_bounded(argv, **kwargs)  # type: ignore[arg-type, return-value]

    def _record_subprocess_run(
        argv: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            cwd = kwargs.get("cwd")
            if isinstance(cwd, (str, Path)):
                git_cwds.append(Path(cwd).resolve())
            elif "-C" in argv:
                git_cwds.append(Path(argv[argv.index("-C") + 1]).resolve())
        return real_subprocess_run(argv, *args, **kwargs)  # type: ignore[arg-type, return-value]

    monkeypatch.setattr(proc, "run_bounded", _record_run_bounded)
    monkeypatch.setattr(memory_repo, "run_bounded", _record_run_bounded)
    monkeypatch.setattr(subprocess, "run", _record_subprocess_run)
    monkeypatch.setattr(_converge, "is_default_home", _is_not_default_home)

    with _stub_host_integrations():
        _converge.converge_host(
            tmp_path / "repo",
            frozenset({"agent-runner"}),
            ava_home=tmp_path,
            steps=_converge.CONVERGE_STEPS,
        )

    assert not [cwd for cwd in git_cwds if cwd.is_relative_to(memory_pool)]


def test_memory_init_returns_a_clean_error_for_the_wrong_branch_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_memory_plugin()
    set_identity(role="agent-runner", name="test-runner")
    _make_dirty_memory_repo(paths.memory_dir(), "main")
    branch_mismatches: list[memory_repo.MemoryBranchMismatch] = []

    def _record_branch_mismatch() -> ScaffoldResult:
        try:
            return run_plugin_scaffolds()
        except memory_repo.MemoryBranchMismatch as exc:
            branch_mismatches.append(exc)
            raise

    monkeypatch.setattr(
        "cli.commands._converge_plugins.run_plugin_scaffolds", _record_branch_mismatch
    )

    assert memory.cmd_memory_init() == 1

    stderr = capsys.readouterr().err
    assert len(branch_mismatches) == 1
    assert f"✗ {branch_mismatches[0]}" in stderr
    assert "Manually switch:" in stderr
    assert "Traceback" not in stderr


def test_memory_init_seeds_a_dirty_correct_branch_pool(capsys: pytest.CaptureFixture[str]) -> None:
    _install_memory_plugin()
    set_identity(role="agent-runner", name="test-runner")
    pool = paths.memory_dir()
    _make_dirty_memory_repo(pool, "machine-test-runner")

    assert memory.cmd_memory_init() == 0

    assert (pool / "MEMORY.md").is_file()
    assert (pool / ".githooks" / "pre-commit").is_file()
    assert (
        subprocess.run(  # noqa: S603
            ["git", "-C", str(pool), "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == ".githooks"
    )
    assert "scaffolded: ava_memory" in capsys.readouterr().out


def test_memory_init_reports_scaffolded_plugins(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "cli.commands._converge_plugins.run_plugin_scaffolds",
        lambda: ScaffoldResult(ran=["example"]),
    )

    assert memory.cmd_memory_init() == 0
    assert "scaffolded: example" in capsys.readouterr().out


def test_memory_init_parser_binds_the_explicit_handler() -> None:
    from cli import main

    args = main._build_parser().parse_args(["memory", "init"])
    assert args.func is main._h_memory_init
