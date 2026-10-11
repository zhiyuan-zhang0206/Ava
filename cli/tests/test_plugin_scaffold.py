"""Explicit plugin scaffolding and its isolation from host convergence."""

import inspect
import shutil
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from base import paths
from base.cluster.machine import set_identity
from base.deploy.git import memory_repo
from base.host import proc
from base.packages.plugins.enable_config import write_local
from base.telemetry import EventPipeline
from cli.commands.converge import host as converge_host
from cli.commands.extensions import memory
from cli.commands.extensions._plugin_scaffold import run_plugin_scaffolds
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    # One home, read through the one seam every consumer shares. `paths.ava_home()` is
    # `AVA_HOME` plus a mkdir, and modules that did `from base.paths import
    # ava_home` (the service roster `converge_host` consults) hold their own reference that a
    # patch of `paths.ava_home` never reaches — so the home lives in the variable, not in a patch,
    # and a second, real `ava_home()` cannot create a sibling directory next to it.
    home = tmp_path / "ava"
    home.mkdir()
    monkeypatch.setenv("AVA_HOME", str(home))


def test_the_isolated_home_is_the_one_every_consumer_resolves(tmp_path: Path) -> None:
    """`ops.roster` binds `ava_home` by name at import, out of reach of any patch of
    `paths.ava_home`; `converge_host` reaches it through `_desired_service_names`. A home that
    only a patch pointed at made that real call create a second, unrelated directory."""
    from ops import roster

    assert roster.ava_home() == paths.ava_home() == tmp_path / "ava"


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


def test_broken_setup_py_is_skipped_and_other_scaffolds_run(
    loguru_records: list[dict],
) -> None:
    """A `setup.py` that fails to import is skipped loudly; the remaining
    plugins still scaffold (fail-soft, user ruling 2026-09-11) — one broken
    plugin must not block the provisioning command for every other plugin."""
    broken = paths.plugins_dir() / "broken"
    broken.mkdir(parents=True)
    (broken / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (broken / "setup.py").write_text("raise RuntimeError('setup boom')\n", encoding="utf-8")
    real = paths.plugins_dir() / "real"
    real.mkdir(parents=True)
    (real / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (real / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold():\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('1')\n",
        encoding="utf-8",
    )
    write_local({"plugins": {"broken": {"enabled": True}, "real": {"enabled": True}}})

    result = run_plugin_scaffolds()  # must not raise

    assert result.ran == ["real"]
    assert (real / "scaffolded.marker").exists()
    assert any(
        "broken" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


def test_scaffold_execution_stays_fail_fast() -> None:
    """Only the `setup.py` IMPORT is fail-soft. A `scaffold()` that raises
    while RUNNING stops provisioning — the documented boundary stays pinned so
    a silent per-plugin skip cannot creep in (user ruling 2026-09-11:
    containment is for load failures, not execution)."""
    pdir = paths.plugins_dir() / "real"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n", encoding="utf-8")
    (pdir / "setup.py").write_text(
        "def scaffold():\n    raise RuntimeError('scaffold boom')\n", encoding="utf-8"
    )
    write_local({"plugins": {"real": {"enabled": True}}})

    with pytest.raises(RuntimeError, match="scaffold boom"):
        run_plugin_scaffolds()


def test_converge_steps_do_not_scaffold_plugins() -> None:
    """Changing this back would reintroduce memory-repository Git work to start."""
    assert "plugin scaffolds" not in {step.name for step in converge_host.CONVERGE_STEPS}

    references = {
        step.name: inspect.getsource(step.apply)
        for step in converge_host.CONVERGE_STEPS
        if "run_plugin_scaffolds" in inspect.getsource(step.apply)
        or "scaffold" in inspect.getsource(step.apply)
    }
    assert references == {}


_HOST_INTEGRATION_STEP_NAMES = frozenset(
    {
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
        "hold watchdog job",
    }
)


def _skip_host_integration(_ctx: converge_host.ConvergeCtx) -> None:
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
    originals: list[tuple[converge_host.ConvergeStep, object]] = []
    try:
        for step in converge_host.CONVERGE_STEPS:
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
    # `git commit` would otherwise spawn a detached `git maintenance run --auto` that holds
    # `.git/objects/maintenance.lock` while the test walks the tree.
    for key, value in (("maintenance.auto", "false"), ("gc.auto", "0")):
        subprocess.run(["git", "-C", str(pool), "config", key, value], check=True)  # noqa: S603
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
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
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
    monkeypatch.setattr(converge_host, "is_default_home", _is_not_default_home)

    with _stub_host_integrations():
        converge_host.converge_host(
            tmp_path / "repo",
            frozenset({"agent-runner"}),
            ava_home=paths.ava_home(),
            steps=converge_host.CONVERGE_STEPS,
            database_factory=operator_database,
            producer=operator_pipeline,
        )

    assert not [cwd for cwd in git_cwds if cwd.is_relative_to(memory_pool)]


def test_memory_init_returns_a_clean_error_for_the_wrong_branch_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_memory_plugin()
    set_identity(role="agent-runner", name="test-runner")
    _make_dirty_memory_repo(paths.memory_dir(), "main")
    assert memory.cmd_memory_init() == 1

    stderr = capsys.readouterr().err
    assert (
        f"✗ {paths.memory_dir()} is already a git repo but on branch 'main', "
        "expected 'machine-test-runner'."
    ) in stderr
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


def test_memory_init_reports_scaffolded_plugins(capsys: pytest.CaptureFixture[str]) -> None:
    plugin = paths.plugins_dir() / "example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text("__description__ = 'test scaffold'\n", encoding="utf-8")
    (plugin / "setup.py").write_text(
        "from pathlib import Path\n"
        "def scaffold():\n"
        "    Path(__file__).with_name('scaffolded.marker').write_text('ready')\n",
        encoding="utf-8",
    )
    write_local({"plugins": {"example": {"enabled": True}}})

    assert memory.cmd_memory_init() == 0
    assert (plugin / "scaffolded.marker").read_text(encoding="utf-8") == "ready"
    assert "scaffolded: example" in capsys.readouterr().out


def test_memory_init_parser_binds_the_explicit_handler() -> None:
    from cli import parsers
    from cli.commands.extensions.parsers import mcp

    args = parsers.parse_args(["memory", "init"])
    assert args.func is mcp._h_memory_init
