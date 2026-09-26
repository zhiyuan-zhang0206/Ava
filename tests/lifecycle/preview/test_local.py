"""Behavioral tests for preview revision selection, isolation and failure cleanup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from scripts.preview import local


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603 — test-owned Git argv
        ["git", "-C", str(repo), *args], text=True
    ).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Preview Test")
    git(path, "config", "user.email", "preview@example.invalid")
    (path / "file").write_text("first")
    git(path, "add", "file")
    git(path, "commit", "-m", "first")
    return path


def test_ref_is_frozen_before_branch_moves(repo: Path, tmp_path: Path) -> None:
    run = local.create(repo, "main", tmp_path / "runs")
    first = git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text("second")
    git(repo, "commit", "-am", "second")
    assert local.resolve_ref(repo, "main") != first
    assert run.data["commit"] == first
    run.command(
        "checkout",
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "--detach",
            str(run.source),
            run.data["commit"],
        ],
        cwd=run.run,
    )
    assert (run.source / "file").read_text() == "first"


def test_clean_environment_cannot_redirect_cluster_or_python(monkeypatch) -> None:
    for key in (
        "AVA_HOME",
        "AVA_DB_URL",
        "AVA_REDIS_URL",
        "AVA_CLUSTER_SECRET",
        "OPENAI_API_KEY",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "NODE_OPTIONS",
        "GIT_DIR",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    env = local.clean_env()
    assert "must-not-inherit" not in env.values()
    assert env["AVA_OS_JOBS_ENABLED"] == "0"
    assert env["AVA_PROVISION_BUILTIN_SCHEDULES"] == "0"


def test_stop_refuses_redirected_home(repo: Path, tmp_path: Path) -> None:
    run = local.create(repo, "HEAD", tmp_path / "runs")
    git(repo, "worktree", "add", "--detach", str(run.source), run.data["commit"])
    (run.source / ".ava_home").write_text(str(tmp_path / "unrelated-home"))
    with pytest.raises(ValueError, match="different home"):
        run.stop()
    assert run.data["steps"] == []


def test_moved_or_symlinked_run_is_not_owned(repo: Path, tmp_path: Path) -> None:
    run = local.create(repo, "HEAD", tmp_path / "runs")
    run.home.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlinks"):
        local.Preview(run.run)
    run.home.unlink()
    data = json.loads(run.manifest.read_text())
    data["run"] = str(tmp_path / "elsewhere")
    local.write_json(run.manifest, data)
    with pytest.raises(ValueError, match="owned preview"):
        local.Preview(run.run)


def test_failed_start_cleans_even_with_keep(repo: Path, tmp_path: Path, monkeypatch) -> None:
    run = local.create(repo, "HEAD", tmp_path / "runs")
    python = run.source / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(run, "prepare", lambda: None)

    def fail():
        raise RuntimeError("start failed")

    monkeypatch.setattr(run, "start", fail)
    stopped = []
    monkeypatch.setattr(run, "stop", lambda: stopped.append(True))
    with pytest.raises(RuntimeError, match="start failed"):
        local.run_preview(run, keep=True)
    assert stopped == [True]
    assert json.loads(run.manifest.read_text())["verification"] == "failed"


def test_failed_teardown_never_reports_clean(repo: Path, tmp_path: Path, monkeypatch) -> None:
    run = local.create(repo, "HEAD", tmp_path / "runs")
    monkeypatch.setattr(run, "assert_checkout", lambda: None)
    monkeypatch.setattr(run, "cli", lambda *_: None)

    def fail(_action):
        raise RuntimeError("listener survived")

    monkeypatch.setattr(run, "runtime", fail)
    with pytest.raises(RuntimeError, match="listener survived"):
        run.stop()
    assert json.loads(run.manifest.read_text())["cleanup"] == "failed"


def test_preview_teardown_never_adds_force_to_normal_stop(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = local.create(repo, "HEAD", tmp_path / "runs")
    run.home.mkdir()
    (run.home / ".env").touch()
    monkeypatch.setattr(run, "assert_checkout", lambda: None)
    calls: list[str] = []

    def call(name: str, args: list[str]) -> None:
        assert "--force" not in args
        calls.append(name)

    monkeypatch.setattr(run, "cli", call)
    monkeypatch.setattr(run, "runtime", calls.append)
    run.stop()
    assert calls == ["stop", "destroy", "verify-stopped"]


def test_timeout_reaps_foreground_descendant(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time; "
        "from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    with (tmp_path / "command.log").open("w") as log, pytest.raises(subprocess.TimeoutExpired):
        local.run_command(
            [sys.executable, "-c", code, str(pidfile)], tmp_path, dict(os.environ), log, 1
        )
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.05)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_profile_survives_bare_start_without_controller_environment(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dotenv import dotenv_values

    from cli import start_intent
    from cli.parsers import build_parser
    from shared import cluster

    preview = local.create(repo, "HEAD", tmp_path / "runs")
    git(repo, "worktree", "add", "--detach", str(preview.source), preview.data["commit"])
    monkeypatch.setattr(start_intent, "_checkout", lambda: preview.source)
    # First-start input parsing precedes Settings and reads the caller environment.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(preview.home))
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_REGISTRY", str(preview.run / "clusters.json"))
    monkeypatch.delitem(os.environ, "AVA_SERVICE_PATH", raising=False)
    git_executable = shutil.which("git")
    assert git_executable is not None
    admitted_path = os.pathsep.join((str(tmp_path / "tools"), str(Path(git_executable).parent)))
    monkeypatch.setenv("PATH", admitted_path)
    # Manager startup carries only identity, not this controller's PROFILE env.
    for key in local.PROFILE:
        monkeypatch.delenv(key, raising=False)

    def port_free(_port: int) -> bool:
        return True

    monkeypatch.setattr(cluster, "_port_free", port_free)

    class PreparedError(Exception):
        pass

    def initialize(name: str, args: list[str]) -> None:
        assert name == "start"
        assert args[args.index("--config-file") + 1] == str(preview.run / "profile.env")
        start_intent.prepare_start(build_parser().parse_args(args))
        raise PreparedError

    monkeypatch.setattr(preview, "cli", initialize)
    with pytest.raises(PreparedError):
        preview.start()
    env_path = preview.home / ".env"
    before = env_path.read_bytes()
    persisted = dotenv_values(env_path)
    assert all(persisted[key] == value for key, value in preview.data["profile"].items())
    assert persisted["AVA_SERVICE_PATH"] == admitted_path
    assert persisted["AVA_MACHINE_HOST"] == "127.0.0.1"
    # The gateway derives the direct browser origin from the app port start
    # reserved; the preview configuration names no origin of its own.
    (record,) = cluster.load_registry(path=preview.run / "clusters.json").values()
    assert persisted["AVA_APP_PORT"] == str(cluster.record_app_port(record))
    assert "AVA_GATEWAY_CORS_ALLOWED_ORIGINS" not in persisted
    monkeypatch.setenv("PATH", str(tmp_path / "manager-tools"))
    start_intent.prepare_start(build_parser().parse_args(["start"]))
    assert env_path.read_bytes() == before


def test_first_business_admission_precedes_observer_check(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = local.create(repo, "HEAD", tmp_path / "runs")
    actions: list[str] = []
    monkeypatch.setattr(preview, "prepare", lambda: None)
    monkeypatch.setattr(preview, "cli", lambda _name, _args: None)
    monkeypatch.setattr(preview, "assert_checkout", lambda: None)

    def runtime(action: str) -> None:
        actions.append(action)
        if action == "describe":
            local.write_json(
                preview.run / "config.json",
                {"frontend_url": "http://frontend", "gateway_url": "http://gateway"},
            )

    monkeypatch.setattr(preview, "runtime", runtime)
    local.run_preview(preview, keep=True)
    assert actions == ["describe", "smoke", "check"]
