"""cli.commands._lgtm — native converge bring-up gating tests.

The local LGTM backends are a host singleton; converge runs on every `ava
start` of every cluster on the box, so the bring-up must fire ONLY on the home
carrying the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from cli.commands import _lgtm, _lgtm_native
from cli.commands._converge_spec import ConvergeCtx


def _fail_on_docker_query(_name: str) -> None:
    pytest.fail("native lifecycle must not query the Docker CLI")


def _ctx(tmp_path: Path) -> ConvergeCtx:
    repo = tmp_path / "repo"
    (repo / "deploy" / "lgtm").mkdir(parents=True)
    return ConvergeCtx(repo=repo, ava_home=tmp_path / "home", roles=frozenset({"gateway"}))


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _native_start_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    user_home = tmp_path / "user"
    agents_dir = user_home / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    ava_home = tmp_path / "ava-home"
    for name in ("loki", "prometheus"):
        _write_executable(ava_home / "lgtm" / "native" / "bin" / name, "#!/bin/sh\nexit 0\n")

    fake_bin = tmp_path / "fake-bin"
    _write_executable(fake_bin / "curl", "#!/bin/sh\nprintf '200'\n")
    launchctl_log = tmp_path / "launchctl.log"
    _write_executable(
        fake_bin / "launchctl",
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$LAUNCHCTL_LOG"\n'
        '[[ "$1" == "print" ]] && exit 1\n'
        "exit 0\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "AVA_HOME": str(ava_home),
            "HOME": str(user_home),
            "LAUNCHCTL_LOG": str(launchctl_log),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    return ava_home, agents_dir, launchctl_log, env


def _run_native_start(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    deploy_dir = Path(__file__).resolve().parents[2] / "deploy" / "lgtm"
    return subprocess.run(  # noqa: S603 — fixed argv executes this repo's start script
        ["bash", str(deploy_dir / "start.sh")],
        cwd=deploy_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_no_marker_never_touches_native_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A home without the lgtm-host marker (every dev worktree cluster) is a
    no-op — it must not touch another home's native backends from its own checkout."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    runs: list[list[str]] = []
    monkeypatch.setattr(_lgtm.subprocess, "run", lambda cmd, **_kw: runs.append(cmd))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert runs == []


def test_marker_runs_start_sh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A marked host runs the idempotent native start script in deploy/lgtm."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()

    calls: list[tuple[list[str], Path]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Result:
        calls.append((cmd, Path(str(kw["cwd"]))))
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert calls == [(["bash", "start.sh"], ctx.repo / "deploy" / "lgtm")]


def test_marker_starts_without_docker_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A marked host starts native jobs without consulting the Docker CLI."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    monkeypatch.setattr(shutil, "which", _fail_on_docker_query)
    runs: list[list[str]] = []

    class _Result:
        returncode = 0

    def record_run(cmd: list[str], **_kw: object) -> _Result:
        runs.append(cmd)
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", record_run)  # pyright: ignore[reportUnknownArgumentType]

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert runs == [["bash", "start.sh"]]


def test_failing_start_sh_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The marker is the operator's statement that this host owns the
    observability backend — a failing bring-up is a real defect, not a skip."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()

    class _Failed:
        returncode = 1

    monkeypatch.setattr(_lgtm.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    with pytest.raises(RuntimeError, match=r"start\.sh exited 1"):
        _lgtm.ensure_lgtm_stack_step(ctx)


def test_native_backend_ports_are_unconditionally_loopback_only() -> None:
    """The native Loki and Prometheus listeners stay pinned to loopback."""
    native = Path(__file__).resolve().parents[2] / "deploy/lgtm/native"
    loki = (native / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 127.0.0.1" in loki
    assert "grpc_listen_address: 127.0.0.1" in loki
    assert "instance_addr: 127.0.0.1" in loki
    assert (
        "--web.listen-address=127.0.0.1:9090"
        in _lgtm_native._NATIVE_CONSTANTS["prometheus"].arguments
    )


def test_native_step_runs_only_for_the_marker_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    calls: list[tuple[Path, Path]] = []

    def record_ensure(repo: Path, home: Path) -> None:
        calls.append((repo, home))

    monkeypatch.setattr(
        _lgtm_native,
        "ensure_lgtm_native",
        record_ensure,
    )

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert calls == [(ctx.repo, ctx.ava_home)]


def test_native_label_and_plist_are_scoped_to_the_cluster_home(tmp_path: Path) -> None:
    from shared.cluster import home_slug

    home = tmp_path / "home"
    label = _lgtm_native.native_label("loki", home)

    assert label == f"com.ava.loki.{home_slug(home)}"
    rendered = plistlib.loads(
        _lgtm_native._render_plist("loki", tmp_path / "native", home).encode()
    )
    assert rendered["Label"] == label


@pytest.mark.parametrize("loki_slugs", [(), ("one", "two")])
def test_lgtm_start_requires_exactly_one_slugged_plist(
    tmp_path: Path, loki_slugs: tuple[str, ...]
) -> None:
    _ava_home, agents_dir, _launchctl_log, env = _native_start_fixture(tmp_path)
    for slug in loki_slugs:
        (agents_dir / f"com.ava.loki.{slug}.plist").touch()
    (agents_dir / "com.ava.prometheus.owner.plist").touch()

    result = _run_native_start(env)

    assert result.returncode != 0
    assert f"found {len(loki_slugs)}" in result.stderr


def test_lgtm_start_bootstraps_the_labels_rendered_by_converge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reachable process left behind by bootout cannot hide an unloaded slugged job."""
    ava_home, agents_dir, launchctl_log, env = _native_start_fixture(tmp_path)

    def no_versions(_repo: Path) -> dict[str, dict[str, str]]:
        return {}

    def no_configs(_repo: Path, _native_dir: Path, _home: Path) -> None:
        return None

    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(_lgtm_native, "_load_versions", no_versions)
    monkeypatch.setattr(_lgtm_native, "_render_configs", no_configs)
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    _lgtm_native.ensure_lgtm_native(Path(__file__).resolve().parents[2], ava_home)

    result = _run_native_start(env)

    assert result.returncode == 0, result.stderr
    launchctl_calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    domain = f"gui/{os.getuid()}"
    for name in ("loki", "prometheus"):
        label = _lgtm_native.native_label(name, ava_home)
        plist = agents_dir / f"{label}.plist"
        assert f"print {domain}/{label}" in launchctl_calls
        assert f"bootstrap {domain} {plist}" in launchctl_calls
        assert f"kickstart {domain}/{label}" in launchctl_calls


def test_native_converge_retires_legacy_and_foreign_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "lgtm-host").touch()
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    current = _lgtm_native.native_label("loki", home)
    legacy = "com.ava.loki"
    foreign = "com.ava.loki.other-home"

    def darwin_arm64() -> str:
        return "darwin_arm64"

    def no_versions(_repo: Path) -> dict[str, dict[str, str]]:
        return {}

    def no_configs(_repo: Path, _native_dir: Path, _home: Path) -> None:
        return None

    monkeypatch.setattr(_lgtm_native, "platform_tag", darwin_arm64)
    monkeypatch.setattr(_lgtm_native, "_load_versions", no_versions)
    monkeypatch.setattr(_lgtm_native, "_render_configs", no_configs)
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "list":
            return subprocess.CompletedProcess(
                command,
                0,
                "-\t0\tcom.apple.unrelated\n"
                f"123\t0\t{legacy}\n"
                f"456\t0\t{foreign}\n"
                f"789\t0\t{current}\n",
                "",
            )
        label = command[-1].rsplit("/", maxsplit=1)[-1]
        return subprocess.CompletedProcess(command, 0 if label in {legacy, foreign} else 1, "", "")

    monkeypatch.setattr(_lgtm_native.subprocess, "run", fake_run)

    _lgtm_native.ensure_lgtm_native(tmp_path / "repo", home)

    assert not (agents_dir / f"{legacy}.plist").exists()
    assert not (agents_dir / f"{foreign}.plist").exists()
    assert (agents_dir / f"{current}.plist").exists()
    assert {call[-1] for call in calls if call[1] == "bootout"} == {
        f"gui/{_lgtm_native.os.getuid()}/{legacy}",
        f"gui/{_lgtm_native.os.getuid()}/{foreign}",
    }
    assert all(current not in call[-1] for call in calls)


def test_bootout_native_jobs_removes_this_homes_slugged_plists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    for name in _lgtm_native._NATIVE_CONSTANTS:
        label = _lgtm_native.native_label(name, home)
        (agents_dir / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label}))
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(_lgtm_native.subprocess, "run", fake_run)

    _lgtm_native.bootout_native_jobs(home)

    assert not list(agents_dir.glob("*.plist"))
    assert [call[1] for call in calls] == ["print", "bootout", "print", "bootout"]
