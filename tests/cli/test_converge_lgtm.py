"""cli.commands._lgtm — native converge bring-up gating tests.

The local LGTM backends are a host singleton; converge runs on every `ava
start` of every cluster on the box, so the bring-up must fire ONLY on the home
carrying the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import copy
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

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


def _empty_native_versions(_repo: Path) -> dict[str, dict[str, str]]:
    return {}


def _native_start_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    user_home = tmp_path / "user"
    agents_dir = user_home / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    ava_home = tmp_path / "ava-home"
    for name in ("loki", "prometheus"):
        _write_executable(ava_home / "lgtm" / "native" / "bin" / name, "#!/bin/sh\nexit 0\n")
    (ava_home / "lgtm" / "native" / "config").mkdir(parents=True)
    (ava_home / "lgtm" / "native" / "config" / "loki.yaml").write_text(
        "auth_enabled: false\n", encoding="utf-8"
    )
    _write_executable(ava_home / "lgtm" / "native" / "grafana" / "run.sh", "#!/bin/sh\nexit 0\n")

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


def test_native_grafana_renders_from_the_repo_and_host_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_query_url",
        "http://tempo.test:3200/",
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint",
        "http://tempo.test:14318/",
    )

    _lgtm_native._render_configs(repo, native_dir, home)
    plist = plistlib.loads(_lgtm_native._render_plist("grafana", native_dir, home).encode())
    grafana_ini = (native_dir / "config/grafana.ini").read_text(encoding="utf-8")
    runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    run_script = (native_dir / "grafana/run.sh").read_text(encoding="utf-8")
    prometheus = yaml.safe_load((native_dir / "config/prometheus.yml").read_text(encoding="utf-8"))

    assert plist["Label"] == _lgtm_native.native_label("grafana", home)
    assert plist["ProgramArguments"] == [str(native_dir.resolve() / "grafana/run.sh")]
    assert plist["EnvironmentVariables"] == {"AVA_HOME": str(home.resolve())}
    assert "{{" not in grafana_ini
    assert "{{" not in runtime_env
    assert "{{" not in run_script
    assert (
        f"GRAFANA_PROVISIONING_PATH={repo}/deploy/lgtm/config/grafana/provisioning/dashboards"
        in runtime_env
    )
    assert "AVA_TELEMETRY_TEMPO_QUERY_URL=http://tempo.test:3200" in runtime_env
    assert "admin_password" in run_script
    assert 'export GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"' in run_script
    assert f"{repo}/deploy/lgtm/.env" in run_script
    assert f"{native_dir}/config/runtime.env" in run_script
    assert str(native_dir / "grafana-home/bin/grafana") in run_script
    assert str(native_dir / "config/grafana.ini") in run_script
    assert str(native_dir / "grafana-home") in run_script
    assert {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }["tempo"] == ["tempo.test:3200"]

    datasources = (
        repo / "deploy/lgtm/config/grafana/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    assert "100.78.137.46" not in datasources
    assert "$__env{AVA_TELEMETRY_TEMPO_QUERY_URL}" in datasources


def test_native_converge_renders_grafana_password_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(_lgtm_native, "_load_versions", _empty_native_versions)
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    # Non-secret fixture; production reads the credential from settings.alerts.grafana_admin_password.
    monkeypatch.setattr(
        "shared.config.settings.alerts.grafana_admin_password",
        SecretStr("fake-key-for-test"),
    )

    _lgtm_native.ensure_lgtm_native(repo, home)

    credential_file = home / "lgtm/native/grafana/admin_password"
    rendered = credential_file.read_text(encoding="utf-8")
    assert rendered == "fake-key-for-test\n"
    file_mode = credential_file.stat().st_mode & 0o777
    assert file_mode == 0o600


def test_native_converge_leaves_unconfigured_grafana_password_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(_lgtm_native, "_load_versions", _empty_native_versions)
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    monkeypatch.setattr("shared.config.settings.alerts.grafana_admin_password", None)

    _lgtm_native.ensure_lgtm_native(repo, home)

    credential_file = home / "lgtm/native/grafana/admin_password"
    run_script = (home / "lgtm/native/grafana/run.sh").read_text(encoding="utf-8")
    assert not credential_file.exists()
    assert f'if [[ -f "{credential_file}" ]]; then' in run_script
    assert f'cat "{credential_file}"' not in run_script


@pytest.mark.parametrize(
    ("query_url", "intake_endpoint", "warns"),
    [
        ("http://127.0.0.1:3200", "http://tempo.example:14318", True),
        ("http://127.0.0.1:3200", "http://localhost:14318", False),
        ("http://tempo.example:3200", "http://collector.example:14318", False),
    ],
)
def test_native_config_warns_only_for_mismatched_tempo_topology(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    query_url: str,
    intake_endpoint: str,
    warns: bool,
) -> None:
    monkeypatch.setattr("shared.config.settings.observability.telemetry_tempo_query_url", query_url)
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", intake_endpoint
    )

    _lgtm_native._render_configs(Path(__file__).resolve().parents[2], tmp_path / "native", tmp_path)

    captured = capsys.readouterr()
    if warns:
        assert "AVA_TELEMETRY_TEMPO_QUERY_URL resolves to http://127.0.0.1:3200" in captured.err
        assert "Tempo intake endpoint is http://tempo.example:14318" in captured.err
    else:
        assert captured.err == ""


def _without_loki_transport_paths(config: dict[str, object]) -> dict[str, object]:
    """Drop the explicitly host/container-specific Loki transport paths."""
    comparable = copy.deepcopy(config)
    for path in (
        ("common", "path_prefix"),
        ("common", "storage", "filesystem", "chunks_directory"),
        ("common", "storage", "filesystem", "rules_directory"),
        ("compactor", "working_directory"),
        ("server", "http_listen_address"),
        ("server", "grpc_listen_address"),
        ("common", "ring", "instance_addr"),
        ("frontend", "address"),
    ):
        parent: dict[str, object] = comparable
        for key in path[:-1]:
            child = parent.get(key)
            if child is None:
                break
            assert isinstance(child, dict)
            parent = child
        else:
            parent.pop(path[-1], None)
    if comparable.get("frontend") == {}:
        comparable.pop("frontend")
    return comparable


def test_native_loki_limits_match_the_container_rollback_config() -> None:
    repo = Path(__file__).resolve().parents[2]
    container = yaml.safe_load((repo / "deploy/lgtm/config/loki.yaml").read_text(encoding="utf-8"))
    native = yaml.safe_load(
        (repo / "deploy/lgtm/native/config/loki.yaml").read_text(encoding="utf-8")
    )

    assert _without_loki_transport_paths(native) == _without_loki_transport_paths(container)


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
    for name in ("loki", "prometheus", "grafana"):
        label = _lgtm_native.native_label(name, ava_home)
        plist = agents_dir / f"{label}.plist"
        assert f"print {domain}/{label}" in launchctl_calls
        assert f"bootstrap {domain} {plist}" in launchctl_calls
        assert f"kickstart {domain}/{label}" in launchctl_calls


@pytest.mark.parametrize("current_loaded", [False, True])
def test_native_converge_retires_legacy_and_foreign_jobs_only_after_current_job_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current_loaded: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "lgtm-host").touch()
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    current = _lgtm_native.native_label("loki", home)
    legacy = "com.ava.loki"
    foreign = "com.ava.loki.other-home"
    legacy_plist = agents_dir / f"{legacy}.plist"
    foreign_plist = agents_dir / f"{foreign}.plist"
    legacy_plist.write_bytes(plistlib.dumps({"Label": legacy}))
    foreign_plist.write_bytes(plistlib.dumps({"Label": foreign}))

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
        loaded = {legacy, foreign}
        if current_loaded:
            loaded.add(current)
        return subprocess.CompletedProcess(command, 0 if label in loaded else 1, "", "")

    monkeypatch.setattr(_lgtm_native.subprocess, "run", fake_run)

    _lgtm_native.ensure_lgtm_native(tmp_path / "repo", home)

    assert legacy_plist.exists() is not current_loaded
    assert foreign_plist.exists() is not current_loaded
    assert (agents_dir / f"{current}.plist").exists()
    booted_out = {call[-1] for call in calls if call[1] == "bootout"}
    expected_bootouts = {
        f"gui/{_lgtm_native.os.getuid()}/{legacy}",
        f"gui/{_lgtm_native.os.getuid()}/{foreign}",
    }
    assert booted_out == (expected_bootouts if current_loaded else set())
    assert all(current not in call[-1] for call in calls if call[1] == "bootout")


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
    assert [call[1] for call in calls] == ["print", "bootout"] * 3
