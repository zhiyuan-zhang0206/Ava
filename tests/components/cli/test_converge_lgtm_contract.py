"""Contract: the rendered LGTM configs equal the repository's deploy/lgtm provisioning files, and the native Loki limits match the container rollback config."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from base.db import Database
from base.telemetry.lgtm_local import service_argv
from cli.commands.observability import lgtm_native


def _skip_binary_verification(_home: Path) -> None:
    """Render fixtures do not install native executable assets."""


@pytest.fixture(autouse=True)
def _darwin_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """These existing lifecycle cases exercise the Darwin implementation."""
    monkeypatch.setattr(lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lgtm_native.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(lgtm_native, "_verify_loki", _skip_binary_verification)


_STUB_RENDER = '{"title": "Ava Ops", "panels": []}\n'


@pytest.fixture(autouse=True)
def _default_provisioning_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-render assertions use an explicit default deployment configuration."""
    monkeypatch.setattr(
        "base.config.settings.data_plane.db_url", "postgresql://reader@127.0.0.1:5433/ava"
    )
    monkeypatch.setattr("base.config.settings.gateway.gateway_url", "")
    monkeypatch.setattr("base.config.settings.gateway.gateway_port", 8000)

    def render_dashboard_json(
        _db: Database, _repo_only: bool = False
    ) -> tuple[str, tuple[str, ...]]:
        return _STUB_RENDER, ()

    monkeypatch.setattr(
        "base.telemetry.metrics.grafana_dashboard_supply.render_dashboard_json",
        render_dashboard_json,
    )


def test_native_backend_listen_hosts_are_settings_rendered_with_loopback_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The native Loki and Prometheus listeners are rendered from
    settings.observability.lgtm_listen_host; the loopback default reproduces the
    pre-parameterization output byte for byte (the contract lock for the
    AVA_LGTM_LISTEN_HOST knob)."""
    native = Path(__file__).resolve().parents[3] / "deploy/lgtm/native"
    loki = (native / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: __LGTM_LISTEN_HOST__" in loki
    assert "grpc_listen_address: __LGTM_LISTEN_HOST__" in loki
    # Single-binary internal addresses stay pinned to loopback by design.
    assert "instance_addr: 127.0.0.1" in loki
    assert "address: 127.0.0.1" in loki
    assert (
        "--web.listen-address={lgtm_listen_host}:{lgtm_prometheus_port}"
        in lgtm_native._NATIVE_CONSTANTS["prometheus"].arguments
    )

    repo = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr("base.config.settings.observability.lgtm_listen_host", "127.0.0.1")
    lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 127.0.0.1" in rendered_loki
    assert "grpc_listen_address: 127.0.0.1" in rendered_loki
    prometheus_argv = service_argv(home, "prometheus")
    assert "--web.listen-address=127.0.0.1:9090" in prometheus_argv

    # A non-loopback setting flows through to the rendered listeners.
    monkeypatch.setattr("base.config.settings.observability.lgtm_listen_host", "10.0.0.5")
    lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 10.0.0.5" in rendered_loki
    assert "grpc_listen_address: 10.0.0.5" in rendered_loki
    prometheus_argv = service_argv(home, "prometheus")
    assert "--web.listen-address=10.0.0.5:9090" in prometheus_argv


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


def _assert_contains(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle in text


def test_native_grafana_renders_from_the_repo_and_host_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_tempo_query_url",
        "http://tempo.test:3200/",
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_tempo_endpoint",
        "http://tempo.test:14318/",
    )

    lgtm_native._render_configs(repo, native_dir, home)
    grafana_ini = (native_dir / "config/grafana.ini").read_text(encoding="utf-8")
    runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    run_script = (native_dir / "grafana/run.sh").read_text(encoding="utf-8")
    prometheus = yaml.safe_load((native_dir / "config/prometheus.yml").read_text(encoding="utf-8"))

    for text in (grafana_ini, runtime_env, run_script):
        assert "{{" not in text
    _assert_contains(
        runtime_env,
        f"GRAFANA_PROVISIONING_PATH={native_dir}/config/provisioning/dashboards",
        "AVA_TELEMETRY_TEMPO_QUERY_URL=http://tempo.test:3200",
    )
    _assert_contains(
        run_script,
        "admin_password",
        'export GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"',
        f"{repo}/deploy/lgtm/.env",
        f"{native_dir}/config/runtime.env",
        str(native_dir / "grafana-home/bin/grafana"),
        str(native_dir / "config/grafana.ini"),
        str(native_dir / "grafana-home"),
    )
    assert {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }["tempo"] == ["tempo.test:3200"]

    # The repo datasources.yml is a template; the rendered copy under the
    # native dir carries the baked URLs (default = loopback, byte-identical
    # to the pre-parameterization content).
    template = (
        repo / "deploy/lgtm/config/grafana/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    _assert_contains(
        template,
        "$__env{AVA_TELEMETRY_LOKI_URL}",
        "$__env{AVA_TELEMETRY_PROMETHEUS_URL}",
        "$__env{AVA_PG_URL}",
        "$__env{AVA_TELEMETRY_TEMPO_QUERY_URL}",
    )
    # The rendered provisioning tree keeps the $__env{} references verbatim;
    # the two-state VALUES are baked into runtime.env (Grafana expands at
    # runtime from its process env).
    rendered_datasources = (
        native_dir / "config/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    assert "$__env{AVA_TELEMETRY_LOKI_URL}" in rendered_datasources
    for text in (template, rendered_datasources):
        assert "{{" not in text
    rendered_runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    _assert_contains(
        rendered_runtime_env,
        "AVA_TELEMETRY_LOKI_URL=http://127.0.0.1:3100",
        "AVA_TELEMETRY_PROMETHEUS_URL=http://127.0.0.1:9090",
        "AVA_PG_URL=127.0.0.1:5433",
        "AVA_ALERTS_WEBHOOK_URL=http://127.0.0.1:8000/api/alerts",
    )
    _assert_rendered_provisioning(native_dir, loki="$__env{AVA_TELEMETRY_LOKI_URL}")


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
        ("server", "http_listen_port"),
        ("server", "grpc_listen_port"),
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
    repo = Path(__file__).resolve().parents[3]
    container = yaml.safe_load((repo / "deploy/lgtm/config/loki.yaml").read_text(encoding="utf-8"))
    native = yaml.safe_load(
        (repo / "deploy/lgtm/native/config/loki.yaml").read_text(encoding="utf-8")
    )

    assert _without_loki_transport_paths(native) == _without_loki_transport_paths(container)
    # Both variants must ship the noise-reducing level (task #1978): the
    # default info writes every flush stream per chunk into the launchd log.
    assert container["server"]["log_level"] == "warn"
    assert native["server"]["log_level"] == "warn"
