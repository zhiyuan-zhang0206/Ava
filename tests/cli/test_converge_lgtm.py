"""cli.commands._lgtm — native converge bring-up gating tests.

The local LGTM backends are a host singleton; converge runs on every `ava
start` of every cluster on the box, so the bring-up must fire ONLY on the home
carrying the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from cli.commands import _lgtm_native, _observatory_urls
from cli.commands._converge_spec import ConvergeCtx
from shared.lgtm_local import service_argv

# S104-flagged literal reused by the mismatch-warning parametrize — a config
# value under test, not a bind.
_WILDCARD_LISTEN = "0.0.0.0"  # noqa: S104


def _skip_binary_verification(_home: Path) -> None:
    """Render fixtures do not install native executable assets."""


@pytest.fixture(autouse=True)
def _darwin_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """These existing lifecycle cases exercise the Darwin implementation."""
    monkeypatch.setattr(_lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_lgtm_native.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(_lgtm_native, "_verify_loki", _skip_binary_verification)


# The fixed document the S3 dashboard render is stubbed to: the assertions
# below pin strict stderr checks on the config renderer, and the real render
# reads the plugin registry (DB) — offline isolation, not behavior under test.
_STUB_RENDER = '{"title": "Ava Ops", "panels": []}\n'


@pytest.fixture(autouse=True)
def _default_provisioning_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-render assertions use an explicit default deployment configuration."""
    monkeypatch.setattr(
        "shared.config.settings.data_plane.db_url", "postgresql://reader@127.0.0.1:5433/ava"
    )
    monkeypatch.setattr("shared.config.settings.gateway.gateway_url", "")
    monkeypatch.setattr("shared.config.settings.gateway.gateway_port", 8000)

    def render_dashboard_json(_repo_only: bool = False) -> tuple[str, tuple[str, ...]]:
        return _STUB_RENDER, ()

    monkeypatch.setattr(
        "shared.metrics.grafana_dashboard_supply.render_dashboard_json", render_dashboard_json
    )


def _fail_on_docker_query(_name: str) -> None:
    pytest.fail("native lifecycle must not query the Docker CLI")


def _ctx(tmp_path: Path) -> ConvergeCtx:
    repo = tmp_path / "repo"
    (repo / "deploy" / "lgtm").mkdir(parents=True)
    return ConvergeCtx(
        repo=repo,
        ava_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
        services=frozenset(_lgtm_native.BACKENDS),
    )


def _empty_native_versions(_repo: Path) -> dict[str, dict[str, str]]:
    return {}


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
        "--web.listen-address={lgtm_listen_host}:{lgtm_prometheus_port}"
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
    prometheus_argv = service_argv(home, "prometheus")
    assert "--web.listen-address=127.0.0.1:9090" in prometheus_argv

    # A non-loopback setting flows through to the rendered listeners.
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", "10.0.0.5")
    _lgtm_native._render_configs(repo, native_dir, home)
    rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 10.0.0.5" in rendered_loki
    assert "grpc_listen_address: 10.0.0.5" in rendered_loki
    prometheus_argv = service_argv(home, "prometheus")
    assert "--web.listen-address=10.0.0.5:9090" in prometheus_argv


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
    # #2048: the native deployment provisions no preinstalled plugins, so the
    # Grafana 13 background installer must be off — its in-flight work can hold
    # a normal SIGTERM past the unit's shutdown deadline (SendSIGKILL=no).
    assert "preinstall_disabled = true" in grafana_ini

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
    grafana_ini = (native_dir / "config/grafana.ini").read_text(encoding="utf-8")
    runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    run_script = (native_dir / "grafana/run.sh").read_text(encoding="utf-8")
    prometheus = yaml.safe_load((native_dir / "config/prometheus.yml").read_text(encoding="utf-8"))

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
    # Non-secret fixture; production reads the credential from settings.alerts.grafana_admin_password.
    monkeypatch.setattr(
        "shared.config.settings.alerts.grafana_admin_password",
        SecretStr("fake-key-for-test"),
    )

    _lgtm_native.ensure_lgtm_native(repo, home, services=frozenset(_lgtm_native.BACKENDS))

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
    monkeypatch.setattr("shared.config.settings.alerts.grafana_admin_password", None)

    _lgtm_native.ensure_lgtm_native(repo, home, services=frozenset(_lgtm_native.BACKENDS))

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

    def record_ensure(repo: Path, home: Path, *, services: frozenset[str]) -> None:
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
        services=frozenset(_lgtm_native.BACKENDS),
    )


def test_native_step_runs_for_station_role_without_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A home declaring the observability-station capability converges the
    native backends with no lgtm-host marker — the marker mechanism is no
    longer required for a role-declared station."""
    ctx = _station_ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    calls: list[tuple[Path, Path]] = []

    def record_ensure(repo: Path, home: Path, *, services: frozenset[str]) -> None:
        calls.append((repo, home))

    monkeypatch.setattr(_lgtm_native, "ensure_lgtm_native", record_ensure)

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert calls == [(ctx.repo, ctx.ava_home)]


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

    _lgtm_native.ensure_lgtm_native(repo, home, services=frozenset(_lgtm_native.BACKENDS))

    native_dir = home / "lgtm/native"
    for name in ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env"):
        assert (native_dir / "config" / name).is_file()
    assert (native_dir / "grafana" / "run.sh").is_file()
    assert not list(agents_dir.iterdir())
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
    prometheus_argv = service_argv(home, "prometheus")
    assert "--storage.tsdb.path=" + str((home / "lgtm/native/data/prom").resolve()) in (
        prometheus_argv
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
    prometheus_argv = service_argv(home, "prometheus")
    assert "--storage.tsdb.path=/data/obs/prom" in prometheus_argv


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
    monkeypatch.setattr("shared.config.settings.observability.lgtm_storage_dir", str(storage))

    _lgtm_native.ensure_lgtm_native(repo, home, services=frozenset(_lgtm_native.BACKENDS))

    assert (storage / "loki").is_dir()
    assert (storage / "prom").is_dir()
    assert (home / "lgtm/native/data").exists() is False
