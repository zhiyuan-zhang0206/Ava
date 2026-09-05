"""cli.commands._lgtm — native converge bring-up gating tests.

The local LGTM backends are a host singleton; converge runs on every `ava
start` of every cluster on the box, so the bring-up must fire ONLY on the home
carrying the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from cli.commands import _lgtm, _lgtm_native, _observatory_urls
from cli.commands._converge_spec import ConvergeCtx

# S104-flagged literal reused by the mismatch-warning parametrize — a config
# value under test, not a bind.
_WILDCARD_LISTEN = "0.0.0.0"  # noqa: S104


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
    monkeypatch.setattr(_lgtm.subprocess, "run", lambda cmd, **_kw: runs.append(cmd))  # pyright: ignore[reportUnknownArgumentType]

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

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)

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

    monkeypatch.setattr(_lgtm.subprocess, "run", record_run)

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

    monkeypatch.setattr(_lgtm.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError, match=r"start\.sh exited 1"):
        _lgtm.ensure_lgtm_stack_step(ctx)


def test_native_backend_listen_hosts_are_settings_rendered_with_loopback_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The native Loki and Prometheus listeners are rendered from
    settings.observability.lgtm_listen_host; the loopback default reproduces the
    pre-parameterization output byte for byte (the contract lock for the
    AVA_LGTM_LISTEN_HOST knob)."""
    native = Path(__file__).resolve().parents[2] / "deploy/lgtm/native"
    loki = (native / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: __LGTM_LISTEN_HOST__" in loki
    assert "grpc_listen_address: __LGTM_LISTEN_HOST__" in loki
    # Single-binary internal addresses stay pinned to loopback by design.
    assert "instance_addr: 127.0.0.1" in loki
    assert "address: 127.0.0.1" in loki
    assert (
        "--web.listen-address={lgtm_listen_host}:9090"
        in _lgtm_native._NATIVE_CONSTANTS["prometheus"].arguments
    )

    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", "127.0.0.1")
    _lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 127.0.0.1" in rendered_loki
    assert "grpc_listen_address: 127.0.0.1" in rendered_loki
    prometheus_plist = plistlib.loads(
        _lgtm_native._render_plist("prometheus", native_dir, home).encode()
    )
    assert "--web.listen-address=127.0.0.1:9090" in prometheus_plist["ProgramArguments"]

    # A non-loopback setting flows through to the rendered listeners.
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", "10.0.0.5")
    _lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 10.0.0.5" in rendered_loki
    assert "grpc_listen_address: 10.0.0.5" in rendered_loki
    prometheus_plist = plistlib.loads(
        _lgtm_native._render_plist("prometheus", native_dir, home).encode()
    )
    assert "--web.listen-address=10.0.0.5:9090" in prometheus_plist["ProgramArguments"]


def test_native_grafana_http_addr_is_settings_rendered_with_all_interfaces_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Grafana's http_addr is rendered from settings.observability.lgtm_grafana_listen_host;
    the 0.0.0.0 default writes out the historical all-interfaces bind explicitly —
    the one byte-level change the parameterization makes (semantics preserved)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(
        "shared.config.settings.observability.lgtm_grafana_listen_host",
        "0.0.0.0",  # noqa: S104 — asserted config default, not a bind
    )
    _lgtm_native._render_configs(repo, native_dir, home)
    grafana_ini = (native_dir / "config/grafana.ini").read_text(encoding="utf-8")
    assert "http_addr = 0.0.0.0" in grafana_ini
    assert "http_port = 3003" in grafana_ini

    monkeypatch.setattr("shared.config.settings.observability.lgtm_grafana_listen_host", "10.0.0.5")
    _lgtm_native._render_configs(repo, native_dir, home)
    grafana_ini = (native_dir / "config/grafana.ini").read_text(encoding="utf-8")
    assert "http_addr = 10.0.0.5" in grafana_ini
    assert "http_port = 3003" in grafana_ini


def _assert_rendered_provisioning(
    native_dir: Path,
    *,
    loki: str | None = None,
    prometheus: str | None = None,
    pg: str | None = None,
    webhook: str | None = None,
) -> None:
    """Parse the converge-rendered provisioning tree and lock the datasource
    and webhook URL values (default rendering output contract)."""
    rendered_datasources = yaml.safe_load(
        (native_dir / "config/provisioning/datasources/datasources.yml").read_text(encoding="utf-8")
    )
    by_uid = {ds["uid"]: ds["url"] for ds in rendered_datasources["datasources"]}
    if loki is not None:
        assert by_uid["loki"] == loki
    if prometheus is not None:
        assert by_uid["prometheus"] == prometheus
    if pg is not None:
        assert by_uid["ops"] == pg
    datasources_text = (native_dir / "config/provisioning/datasources/datasources.yml").read_text(
        encoding="utf-8"
    )
    assert "{{" not in datasources_text
    rendered_contact = yaml.safe_load(
        (native_dir / "config/provisioning/alerting/contact.yml").read_text(encoding="utf-8")
    )
    if webhook is not None:
        assert rendered_contact["contactPoints"][0]["receivers"][0]["settings"]["url"] == webhook
    contact_text = (native_dir / "config/provisioning/alerting/contact.yml").read_text(
        encoding="utf-8"
    )
    assert "{{" not in contact_text


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
    assert f"GRAFANA_PROVISIONING_PATH={native_dir}/config/provisioning/dashboards" in runtime_env
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

    # The repo datasources.yml is a template; the rendered copy under the
    # native dir carries the baked URLs (default = loopback, byte-identical
    # to the pre-parameterization content).
    template = (
        repo / "deploy/lgtm/config/grafana/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    assert "$__env{AVA_TELEMETRY_LOKI_URL}" in template
    assert "$__env{AVA_TELEMETRY_PROMETHEUS_URL}" in template
    assert "$__env{AVA_PG_URL}" in template
    assert "$__env{AVA_TELEMETRY_TEMPO_QUERY_URL}" in template
    assert "{{" not in template
    # The rendered provisioning tree keeps the $__env{} references verbatim;
    # the two-state VALUES are baked into runtime.env (Grafana expands at
    # runtime from its process env).
    rendered_datasources = (
        native_dir / "config/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    assert "$__env{AVA_TELEMETRY_LOKI_URL}" in rendered_datasources
    assert "{{" not in rendered_datasources
    rendered_runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    assert "AVA_TELEMETRY_LOKI_URL=http://127.0.0.1:3100" in rendered_runtime_env
    assert "AVA_TELEMETRY_PROMETHEUS_URL=http://127.0.0.1:9090" in rendered_runtime_env
    assert "AVA_PG_URL=127.0.0.1:5433" in rendered_runtime_env
    assert "AVA_ALERTS_WEBHOOK_URL=http://127.0.0.1:8000/api/alerts" in rendered_runtime_env
    _assert_rendered_provisioning(native_dir, loki="$__env{AVA_TELEMETRY_LOKI_URL}")


def test_native_provisioning_renders_remote_observatory_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AVA_OBSERVABILITY_URL set -> the datasources + alert webhook render the
    remote observatory endpoints; unset -> the current loopback defaults
    (locked by test_native_grafana_renders_from_the_repo_and_host_setting)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(
        "shared.config.settings.observability.observability_url",
        "http://10.0.0.46",
    )
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.10")
    monkeypatch.setattr(
        "shared.config.settings.data_plane.db_url",
        "postgresql://grafana_ro@10.0.0.72:5433/ava_main",
    )

    _lgtm_native._render_configs(repo, native_dir, home)

    rendered_runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    assert "AVA_TELEMETRY_LOKI_URL=http://10.0.0.46:3100" in rendered_runtime_env
    assert "AVA_TELEMETRY_PROMETHEUS_URL=http://10.0.0.46:9090" in rendered_runtime_env
    # PG is the cluster's own database (#3606): it follows the data-plane
    # db_url, NOT the observatory — stage C moves the observatory while PG
    # stays on the gateway.
    assert "AVA_PG_URL=10.0.0.72:5433" in rendered_runtime_env
    assert "AVA_ALERTS_WEBHOOK_URL=http://10.0.0.10:8000/api/alerts" in rendered_runtime_env
    _assert_rendered_provisioning(
        native_dir,
        loki="$__env{AVA_TELEMETRY_LOKI_URL}",
        prometheus="$__env{AVA_TELEMETRY_PROMETHEUS_URL}",
        pg="$__env{AVA_PG_URL}",
        webhook="$__env{AVA_ALERTS_WEBHOOK_URL}",
    )


def test_native_provisioning_webhook_stays_loopback_without_observatory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No observatory -> the webhook stays byte-identical 127.0.0.1:8000 even
    when reachable_host() would resolve to a tailnet address — self-dialing
    a tailnet IP from the gateway host can hit VPN hairpin filtering."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(
        "shared.config.settings.observability.observability_url",
        "",
    )
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.10")

    _lgtm_native._render_configs(repo, native_dir, home)

    rendered_runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    assert "AVA_ALERTS_WEBHOOK_URL=http://127.0.0.1:8000/api/alerts" in rendered_runtime_env
    assert "10.0.0.10" not in rendered_runtime_env
    _assert_rendered_provisioning(native_dir, webhook="$__env{AVA_ALERTS_WEBHOOK_URL}")


def test_native_provisioning_preserves_user_edited_rendered_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rendered provisioning file the user hand-edited is warned about and
    preserved on the next converge — never overwritten (web-sources precedent)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"

    _lgtm_native._render_configs(repo, native_dir, home)
    datasources = native_dir / "config/provisioning/datasources/datasources.yml"
    # The rendered tree carries $__env{} references (URLs live in runtime.env),
    # so a meaningful user edit replaces a reference with a hardcoded URL.
    user_edit = datasources.read_text(encoding="utf-8").replace(
        "$__env{AVA_TELEMETRY_LOKI_URL}", "http://user.example:3100"
    )
    assert user_edit != datasources.read_text(encoding="utf-8")
    datasources.write_text(user_edit, encoding="utf-8")

    _lgtm_native._render_configs(repo, native_dir, home)

    assert "http://user.example:3100" in datasources.read_text(encoding="utf-8")
    assert "$__env{AVA_TELEMETRY_LOKI_URL}" not in datasources.read_text(encoding="utf-8")
    assert "modified locally" in capsys.readouterr().err


def test_native_provisioning_removes_stale_rendered_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rendered file whose source template vanished is removed when untouched
    (pure derived state) — matching the web-sources cleanup rule."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"

    _lgtm_native._render_configs(repo, native_dir, home)
    stale = native_dir / "config/provisioning/datasources/old.yml"
    stale.write_text("stale", encoding="utf-8")
    hashes_path = native_dir / "config/provisioning-hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    hashes["datasources/old.yml"] = hashlib.sha256(b"stale").hexdigest()
    hashes_path.write_text(json.dumps(hashes), encoding="utf-8")

    _lgtm_native._render_configs(repo, native_dir, home)

    assert not stale.exists()


def test_ensure_kickstarts_grafana_when_config_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A converge that changes the rendered grafana config (INI, runtime env,
    or provisioning tree) kicks the running Grafana so the new config takes
    effect — a running instance never re-reads its INI (QA P1)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    (native_dir / "config").mkdir(parents=True)
    monkeypatch.setattr(
        _lgtm_native,
        "platform_tag",
        lambda: "darwin_arm64",
    )
    monkeypatch.setattr(
        _lgtm_native,
        "_load_versions",
        lambda _repo: {},  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _lgtm_native,
        "_download_and_verify",
        lambda *_a, **_k: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _lgtm_native,
        "_agents_dir",
        lambda: tmp_path / "plists",
    )
    monkeypatch.setattr("shared.config.settings.alerts.grafana_admin_password", None)
    calls: list[list[str]] = []

    def fake_launchctl(*args: str) -> object:
        calls.append(list(args))
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(_lgtm_native, "_launchctl", fake_launchctl)
    monkeypatch.setattr(
        _lgtm_native, "platform", type("P", (), {"system": staticmethod(lambda: "Darwin")})()
    )

    _lgtm_native.ensure_lgtm_native(repo, home)

    kickstarts = [c for c in calls if c[0] == "kickstart"]
    assert len(kickstarts) == 1, kickstarts
    assert kickstarts[0][1] == "-k"
    assert kickstarts[0][2] == f"gui/{os.getuid()}/{_lgtm_native.native_label('grafana', home)}"
    assert "kickstarted" in capsys.readouterr().err


def test_ensure_no_kickstart_when_config_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Steady state: a converge that renders nothing new leaves the running
    Grafana alone (kickstart only on change)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    (native_dir / "config").mkdir(parents=True)
    monkeypatch.setattr(
        _lgtm_native,
        "platform_tag",
        lambda: "darwin_arm64",
    )
    monkeypatch.setattr(
        _lgtm_native,
        "_load_versions",
        lambda _repo: {},  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _lgtm_native,
        "_download_and_verify",
        lambda *_a, **_k: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _lgtm_native,
        "_agents_dir",
        lambda: tmp_path / "plists",
    )
    monkeypatch.setattr("shared.config.settings.alerts.grafana_admin_password", None)
    calls: list[list[str]] = []

    def fake_launchctl(*args: str) -> object:
        calls.append(list(args))
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(_lgtm_native, "_launchctl", fake_launchctl)
    monkeypatch.setattr(
        _lgtm_native, "platform", type("P", (), {"system": staticmethod(lambda: "Darwin")})()
    )

    _lgtm_native.ensure_lgtm_native(repo, home)
    first = len(calls)
    _lgtm_native.ensure_lgtm_native(repo, home)

    kickstarts = [c for c in calls[first:] if c[0] == "kickstart"]
    assert kickstarts == []


def test_native_provisioning_pg_stays_on_data_plane_when_db_url_is_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Remote observatory + a db_url that still names loopback -> the PG
    datasource renders loopback (Grafana would dial its own host) and the
    render warns instead of silently pointing at the observatory (CTO
    review of 3b523ab14; #3606's PG never follows the observatory)."""
    monkeypatch.setattr(
        "shared.config.settings.observability.observability_url",
        "http://10.0.0.46",
    )
    monkeypatch.setattr(
        "shared.config.settings.data_plane.db_url",
        "postgresql:///ava_main?host=/tmp/ava-pg-ava-test&port=5433",
    )
    loki, prometheus, pg = _observatory_urls._observability_datasource_urls()
    assert loki == "http://10.0.0.46:3100"
    assert prometheus == "http://10.0.0.46:9090"
    assert pg == "127.0.0.1:5433"
    assert "data-plane db_url" in capsys.readouterr().err


def test_observability_url_validation_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed AVA_OBSERVABILITY_URL is warned about and falls back to the
    loopback endpoints instead of silently rendering broken URLs (QA P3)."""
    monkeypatch.setattr(
        "shared.config.settings.observability.observability_url",
        "10.0.0.1:1234",  # no scheme — malformed
    )
    loki, prometheus, pg = _observatory_urls._observability_datasource_urls()
    assert loki == "http://127.0.0.1:3100"
    assert prometheus == "http://127.0.0.1:9090"
    assert pg == "127.0.0.1:5433"
    assert "malformed" in capsys.readouterr().err


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


@pytest.mark.parametrize(
    ("listen_host", "grafana_listen_host", "expected_warns"),
    [
        # A specific non-loopback listen host with default loopback read URLs.
        ("10.0.0.5", _WILDCARD_LISTEN, ["AVA_TELEMETRY_LOKI_URL", "AVA_TELEMETRY_PROMETHEUS_URL"]),
        # Both knobs widened, loopback read URLs still in place.
        (
            "10.0.0.5",
            "10.0.0.6",
            ["AVA_TELEMETRY_LOKI_URL", "AVA_TELEMETRY_PROMETHEUS_URL", "AVA_TELEMETRY_GRAFANA_URL"],
        ),
        # Wildcard binds still answer on loopback — no warning.
        (_WILDCARD_LISTEN, _WILDCARD_LISTEN, []),
        # Loopback binds — no warning.
        ("127.0.0.1", "127.0.0.1", []),
        # Listen hostname is not judged — no warning.
        ("tailscale-box", "tailscale-box", []),
    ],
)
def test_native_config_warns_when_widened_listen_host_has_loopback_read_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    listen_host: str,
    grafana_listen_host: str,
    expected_warns: list[str],
) -> None:
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", listen_host)
    monkeypatch.setattr(
        "shared.config.settings.observability.lgtm_grafana_listen_host", grafana_listen_host
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_grafana_url", "http://127.0.0.1:3003"
    )
    # Pin the tempo topology to loopback so its pre-existing warning cannot
    # pollute this test's stderr assertions.
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_query_url", "http://127.0.0.1:3200"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )

    _lgtm_native._render_configs(Path(__file__).resolve().parents[2], tmp_path / "native", tmp_path)

    captured = capsys.readouterr()
    for env_var in expected_warns:
        assert env_var in captured.err
        assert "listens on" in captured.err
    if not expected_warns:
        assert captured.err == ""
    else:
        assert len(captured.err.strip().splitlines()) == len(expected_warns)


def test_native_config_warns_only_when_read_urls_stay_loopback_after_widening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the read URLs follow the widened listen host, the mismatch warning
    stays silent (the external-migration form)."""
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", "10.0.0.5")
    monkeypatch.setattr(
        "shared.config.settings.observability.lgtm_grafana_listen_host", _WILDCARD_LISTEN
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://10.0.0.5:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://10.0.0.5:9090"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_grafana_url", "http://127.0.0.1:3003"
    )
    # Pin the tempo topology to loopback so its pre-existing warning cannot
    # pollute this test's stderr assertion.
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_query_url", "http://127.0.0.1:3200"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )

    _lgtm_native._render_configs(Path(__file__).resolve().parents[2], tmp_path / "native", tmp_path)

    captured = capsys.readouterr()
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
    # Both variants must ship the noise-reducing level (task #1978): the
    # default info writes every flush stream per chunk into the launchd log.
    assert container["server"]["log_level"] == "warn"
    assert native["server"]["log_level"] == "warn"


def test_native_step_runs_only_for_the_marker_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    calls: list[tuple[Path, Path]] = []

    def record_ensure(repo: Path, home: Path, *, station: bool = False) -> None:
        calls.append((repo, home))

    monkeypatch.setattr(
        _lgtm_native,
        "ensure_lgtm_native",
        record_ensure,
    )

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert calls == [(ctx.repo, ctx.ava_home)]


def _station_ctx(tmp_path: Path) -> ConvergeCtx:
    """A converge context for a second machine declaring observability-station
    (no lgtm-host marker) — the WP1 deployment-unit form."""
    repo = tmp_path / "repo"
    (repo / "deploy" / "lgtm").mkdir(parents=True)
    return ConvergeCtx(
        repo=repo,
        ava_home=tmp_path / "station-home",
        roles=frozenset({"observability-station"}),
    )


def test_native_step_runs_for_station_role_without_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A home declaring the observability-station capability converges the
    native backends with no lgtm-host marker — the marker mechanism is no
    longer required for a role-declared station."""
    ctx = _station_ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    calls: list[tuple[Path, Path, bool]] = []

    def record_ensure(repo: Path, home: Path, *, station: bool = False) -> None:
        calls.append((repo, home, station))

    monkeypatch.setattr(_lgtm_native, "ensure_lgtm_native", record_ensure)

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert calls == [(ctx.repo, ctx.ava_home, True)]


def test_stack_step_runs_for_station_role_without_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The observability-station capability also gates the stack bring-up
    (start.sh), marker-free."""
    ctx = _station_ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd: list[str], **_kw: object) -> _Result:
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)

    _lgtm.ensure_lgtm_stack_step(ctx)

    assert calls == [["bash", "start.sh"]]


def _no_launchctl(*_args: str) -> subprocess.CompletedProcess[str]:
    """A launchctl that reports nothing loaded (non-macOS CI has no binary)."""
    return subprocess.CompletedProcess(["launchctl"], 1, "", "")


def test_station_role_renders_full_native_set_without_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dry-run: a second machine declaring the station role renders the FULL
    native set — configs, launchd plists, and storage dirs — with no marker
    and no version downloads (the WP1 acceptance render)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "station-home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(_lgtm_native, "_load_versions", _empty_native_versions)
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(_lgtm_native, "_launchctl", _no_launchctl)

    _lgtm_native.ensure_lgtm_native(repo, home, station=True)

    native_dir = home / "lgtm/native"
    for name in ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env"):
        assert (native_dir / "config" / name).is_file()
    assert (native_dir / "grafana" / "run.sh").is_file()
    for name in ("loki", "prometheus", "grafana"):
        label = _lgtm_native.native_label(name, home)
        assert (agents_dir / f"{label}.plist").is_file()
    assert (native_dir / "data" / "loki").is_dir()
    assert (native_dir / "data" / "prom").is_dir()
    rendered_loki = (native_dir / "config" / "loki.yaml").read_text(encoding="utf-8")
    # No unsubstituted render token survives (the template's {{...}} comment
    # prose is the only legit brace text).
    assert "{{AVA_HOME}}" not in rendered_loki
    assert "{{LGTM_STORAGE_DIR}}" not in rendered_loki


def test_native_storage_dir_default_matches_historical_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty AVA_LGTM_STORAGE_DIR renders the historical
    $AVA_HOME/lgtm/native/data paths byte-for-byte — the macmini re-render
    diff stays empty (the storage-parameterization zero-regression contract)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr("shared.config.settings.observability.lgtm_storage_dir", "")
    _lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = yaml.safe_load((native_dir / "config/loki.yaml").read_text(encoding="utf-8"))
    assert rendered_loki["common"]["path_prefix"] == str((home / "lgtm/native/data/loki").resolve())
    assert rendered_loki["compactor"]["working_directory"] == str(
        (home / "lgtm/native/data/loki/compactor").resolve()
    )
    prometheus_plist = plistlib.loads(
        _lgtm_native._render_plist("prometheus", native_dir, home).encode()
    )
    assert (
        "--storage.tsdb.path=" + str((home / "lgtm/native/data/prom").resolve())
        in (prometheus_plist["ProgramArguments"])
    )


def test_native_storage_dir_parameterized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A per-machine AVA_LGTM_STORAGE_DIR moves the Loki filesystem store and
    the Prometheus TSDB onto the configured data volume."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr("shared.config.settings.observability.lgtm_storage_dir", "/data/obs")
    _lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = yaml.safe_load((native_dir / "config/loki.yaml").read_text(encoding="utf-8"))
    assert rendered_loki["common"]["path_prefix"] == "/data/obs/loki"
    assert (
        rendered_loki["common"]["storage"]["filesystem"]["chunks_directory"]
        == "/data/obs/loki/chunks"
    )
    assert rendered_loki["compactor"]["working_directory"] == "/data/obs/loki/compactor"
    prometheus_plist = plistlib.loads(
        _lgtm_native._render_plist("prometheus", native_dir, home).encode()
    )
    assert "--storage.tsdb.path=/data/obs/prom" in prometheus_plist["ProgramArguments"]


def test_station_role_creates_configured_storage_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The converge render creates the configured storage root plus the loki
    and prom subdirs (start.sh parity for a custom data volume)."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "station-home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    storage = tmp_path / "obs-data"
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(_lgtm_native, "_load_versions", _empty_native_versions)
    monkeypatch.setattr(_lgtm_native, "_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(_lgtm_native, "_launchctl", _no_launchctl)
    monkeypatch.setattr("shared.config.settings.observability.lgtm_storage_dir", str(storage))

    _lgtm_native.ensure_lgtm_native(repo, home, station=True)

    assert (storage / "loki").is_dir()
    assert (storage / "prom").is_dir()
    assert (home / "lgtm/native/data").exists() is False


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
