"""Native LGTM backend installer and launchd rendering tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from cli.commands import _lgtm_native
from cli.commands._converge_spec import ConvergeCtx
from shared import resilience
from shared.config import settings
from shared.lgtm_local import BACKENDS, backend_urls, service_argv
from shared.loki_index_labels import validate_loki_deploy_config

_REAL_VERIFY_LOKI = _lgtm_native._verify_loki


def _skip_binary_verification(_home: Path) -> None:
    """Render fixtures do not install native executable assets."""


@pytest.fixture(autouse=True)
def _darwin_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """These existing lifecycle cases exercise the Darwin implementation."""
    monkeypatch.setattr(_lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_lgtm_native.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(_lgtm_native, "_verify_loki", _skip_binary_verification)


def _repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _native_dir(home: Path) -> Path:
    return home / "lgtm" / "native"


def _mark_current(home: Path) -> None:
    native_dir = _native_dir(home)
    for name, spec in _lgtm_native._load_versions(_repo()).items():
        (native_dir / f"version-{name}").parent.mkdir(parents=True, exist_ok=True)
        (native_dir / f"version-{name}").write_text(spec["version"] + "\n", encoding="utf-8")


# The fixed document the S3 dashboard render is stubbed to: these converge
# tests stay offline (no plugin imports, no registry database).
_STUB_RENDER = '{"title": "Ava Ops", "panels": []}\n'


@pytest.fixture(autouse=True)
def _stub_dashboard_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def render_dashboard_json(_repo_only: bool = False) -> tuple[str, tuple[str, ...]]:
        return _STUB_RENDER, ()

    monkeypatch.setattr(
        "shared.metrics.grafana_dashboard_supply.render_dashboard_json", render_dashboard_json
    )


def test_versions_file_has_the_pinned_release_assets() -> None:
    versions_path = _repo() / "deploy/lgtm/native/versions.yml"
    versions = yaml.safe_load(versions_path.read_text(encoding="utf-8"))

    darwin_versions = {
        name: {
            "version": spec["version"],
            "assets": {"darwin-arm64": spec["assets"]["darwin-arm64"]},
        }
        for name, spec in versions.items()
    }
    assert darwin_versions == {
        "loki": {
            "version": "3.7.6",
            "assets": {
                "darwin-arm64": {
                    "url": "https://github.com/grafana/loki/releases/download/v3.7.6/loki-darwin-arm64.zip",
                    "sha256": "c189a879f040c823b815051ccbc145a23f6799cb531d619a06c0e8cce7076826",
                }
            },
        },
        "prometheus": {
            "version": "3.13.2",
            "assets": {
                "darwin-arm64": {
                    "url": "https://github.com/prometheus/prometheus/releases/download/v3.13.2/prometheus-3.13.2.darwin-arm64.tar.gz",
                    "sha256": "f68ca4f1dbedd6366bbfdd8ac5d2c0b7ba1f273474acc8d38eb33202fbeec7a4",
                }
            },
        },
        "grafana": {
            "version": "13.1.3",
            "assets": {
                "darwin-arm64": {
                    "url": "https://dl.grafana.com/oss/release/grafana-13.1.3.darwin-arm64.tar.gz",
                    "sha256": "cbd4fc856fa5817a7fbc141d1e11cb1d79ca21cea15294cd32d9c82a666d382a",
                }
            },
        },
    }


def test_platform_tag_supports_darwin_arm64_and_linux_amd64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_lgtm_native.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(_lgtm_native, "_verify_loki", _skip_binary_verification)
    assert _lgtm_native.platform_tag() == "darwin_arm64"

    monkeypatch.setattr(_lgtm_native.platform, "system", lambda: "Linux")
    assert _lgtm_native.platform_tag() is None
    monkeypatch.setattr(_lgtm_native.platform, "machine", lambda: "x86_64")
    assert _lgtm_native.platform_tag() == "linux_amd64"


def test_render_warns_when_a_value_diverges_from_the_env_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rendered host-scope value disagreeing with the unit's .env is called
    out — the divergence mode behind the 2026-09-14 tempo revert (task #3339)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "AVA_TELEMETRY_TEMPO_QUERY_URL=http://127.0.0.1:3200\n", encoding="utf-8"
    )
    _lgtm_native._warn_env_file_divergence(
        home, {"AVA_TELEMETRY_TEMPO_QUERY_URL": "http://10.55.0.9:3200"}
    )
    err = capsys.readouterr().err
    assert "AVA_TELEMETRY_TEMPO_QUERY_URL resolved to http://10.55.0.9:3200" in err
    assert "inherited environment value is in effect" in err


def test_render_value_divergence_check_stays_quiet_when_consistent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "AVA_TELEMETRY_TEMPO_QUERY_URL=http://127.0.0.1:3200\nAVA_LGTM_LOKI_PORT=53100\n",
        encoding="utf-8",
    )
    _lgtm_native._warn_env_file_divergence(
        home,
        {
            "AVA_TELEMETRY_TEMPO_QUERY_URL": "http://127.0.0.1:3200",
            "AVA_LGTM_LOKI_PORT": "53100",
            "AVA_LGTM_GRAFANA_PORT": "53003",  # undeclared in the file: skipped
        },
    )
    assert capsys.readouterr().err == ""


def test_ensure_skips_download_when_markers_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _mark_current(home)
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")

    def fail_download(_name: str, _version: str, _asset: dict[str, str], _native_dir: Path) -> None:
        pytest.fail("current marker must skip the download")

    monkeypatch.setattr(
        _lgtm_native,
        "_download_and_verify",
        fail_download,
    )

    _lgtm_native.ensure_lgtm_native(_repo(), home, services=frozenset(_lgtm_native.BACKENDS))

    assert (home / "lgtm/native/config/loki.yaml").exists()


def test_ensure_downloads_when_a_marker_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _mark_current(home)
    (_native_dir(home) / "version-loki").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    downloads: list[str] = []

    def record_download(
        name: str, _version: str, _asset: dict[str, str], _native_dir: Path
    ) -> None:
        downloads.append(name)

    monkeypatch.setattr(
        _lgtm_native,
        "_download_and_verify",
        record_download,
    )

    _lgtm_native.ensure_lgtm_native(_repo(), home, services=frozenset(_lgtm_native.BACKENDS))

    assert downloads == ["loki"]


def test_download_refuses_an_archive_with_the_wrong_sha256(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "native"

    def fake_download(_url: str, archive: Path) -> None:
        archive.write_bytes(b"untrusted")

    monkeypatch.setattr(_lgtm_native, "_download_with_retry", fake_download)

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        _lgtm_native._download_and_verify(
            "loki",
            "3.7.6",
            {"url": "https://example.invalid/loki.zip", "sha256": "0" * 64},
            destination,
        )


def test_download_retry_preserves_sleep_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fail_then_succeed(_url: str, _archive: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bad payload")

    monkeypatch.setattr(_lgtm_native, "_stream_download", fail_then_succeed)
    monkeypatch.setattr(_lgtm_native.time, "sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    monkeypatch.setattr(_lgtm_native.time, "monotonic", lambda: 10.0)
    _lgtm_native._download_with_retry("https://example.invalid/loki.zip", tmp_path / "loki.zip")

    assert calls == 2
    assert sleeps == [5.0]
    assert capsys.readouterr().err == (
        "  ! lgtm native: download attempt 1/3 failed after 0s: bad payload\n"
    )


def test_download_retry_preserves_final_error_and_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    errors = [OSError(f"reset {i}") for i in range(1, 4)]
    sleeps: list[float] = []

    def fail(_url: str, _archive: Path) -> None:
        raise errors.pop(0)

    final_error = errors[-1]
    monkeypatch.setattr(_lgtm_native, "_stream_download", fail)
    monkeypatch.setattr(_lgtm_native.time, "sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    monkeypatch.setattr(_lgtm_native.time, "monotonic", lambda: 10.0)
    url = "https://example.invalid/loki.zip"
    with pytest.raises(RuntimeError) as caught:
        _lgtm_native._download_with_retry(url, tmp_path / "loki.zip")

    assert str(caught.value) == (
        f"failed to download native LGTM backend from {url} after 3 attempts (0s total): reset 3"
    )
    assert caught.value.__cause__ is final_error
    assert sleeps == [5.0, 10.0]
    assert capsys.readouterr().err == "".join(
        f"  ! lgtm native: download attempt {i}/3 failed after 0s: reset {i}\n" for i in range(1, 4)
    )


def test_ensure_renders_configs_with_native_paths_and_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _mark_current(home)
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    # Pin the listen-host and read-URL settings to their defaults so the
    # rendered bytes are deterministic regardless of the runner's environment.
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", "127.0.0.1")
    monkeypatch.setattr(
        "shared.config.settings.observability.lgtm_grafana_listen_host",
        "0.0.0.0",  # noqa: S104 — pinned config default, not a bind
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
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_query_url", "http://127.0.0.1:3200"
    )

    _lgtm_native.ensure_lgtm_native(_repo(), home, services=frozenset(_lgtm_native.BACKENDS))

    config_dir = _native_dir(home) / "config"
    loki = (config_dir / "loki.yaml").read_text(encoding="utf-8")
    prometheus = (config_dir / "prometheus.yml").read_text(encoding="utf-8")
    loki_config = yaml.safe_load(loki)
    prometheus_config = yaml.safe_load(prometheus)
    assert "{{AVA_HOME}}" not in loki
    assert {path.name for path in config_dir.iterdir()} == {
        "grafana.ini",
        "loki.yaml",
        "prometheus.yml",
        "runtime.env",
        # Converge-copied Grafana provisioning tree + its hash sidecar
        # (task #1791 A3: datasource/webhook URLs are $__env{} references
        # resolved from the rendered runtime.env).
        "provisioning",
        "provisioning-hashes.json",
    }
    assert (config_dir / "provisioning/datasources/datasources.yml").is_file()
    assert (config_dir / "provisioning/alerting/contact.yml").is_file()
    assert (config_dir / "provisioning/alerting/rules.yml").is_file()
    # The dashboard is generated from the render path, not copied (S3).
    assert (config_dir / "provisioning/dashboards/ava-ops-main.json").read_text(
        encoding="utf-8"
    ) == _STUB_RENDER
    assert loki_config["common"]["path_prefix"] == f"{home}/lgtm/native/data/loki"
    assert loki_config["frontend"]["address"] == "127.0.0.1"
    assert (
        loki_config["distributor"]
        == yaml.safe_load((_repo() / "deploy/lgtm/config/loki.yaml").read_text(encoding="utf-8"))[
            "distributor"
        ]
    )
    assert "http_listen_address: 127.0.0.1" in loki
    assert "grpc_listen_address: 127.0.0.1" in loki
    assert "instance_addr: 127.0.0.1" in loki
    assert "retention_period: 84h" in loki
    assert "disk_full_threshold: 0.95" in loki
    validate_loki_deploy_config(loki_config)
    assert "max_query_series: 20000" in loki
    assert "query_timeout: 50s" in loki
    assert "max_entries_limit_per_query: 50001" in loki
    assert "split_queries_by_interval: 24h" in loki
    targets = {
        job["job_name"]: job["static_configs"][0]["targets"]
        for job in prometheus_config["scrape_configs"]
    }
    assert targets == {
        "prometheus": ["127.0.0.1:9090"],
        "tempo": ["127.0.0.1:3200"],
        "loki": ["127.0.0.1:3100"],
        "grafana": ["127.0.0.1:3003"],
    }

    grafana_job = next(
        job for job in prometheus_config["scrape_configs"] if job["job_name"] == "grafana"
    )
    assert grafana_job["metrics_path"] == "/grafana/metrics"


def test_lgtm_prometheus_copies_keep_the_late_sample_window() -> None:
    for relative in ("native/config/prometheus.yml", "config/prometheus.yml"):
        config = yaml.safe_load((_repo() / "deploy/lgtm" / relative).read_text(encoding="utf-8"))
        assert config["storage"]["tsdb"]["out_of_order_time_window"] == "6h"


def test_prometheus_too_old_samples_rule_uses_window_delta_in_fast_group() -> None:
    """R24 (task #4650) — focused coverage lives here because
    tests/scripts/test_alert_rules.py is structure-budget frozen."""
    rules = yaml.safe_load(
        (_repo() / "deploy/lgtm/config/grafana/provisioning/alerting/rules.yml").read_text(
            encoding="utf-8"
        )
    )
    group = next(g for g in rules["groups"] if g["name"] == "ava-ops")
    rule = next(r for r in group["rules"] if r["uid"] == "ava-ops-prom-too-old-samples")
    expr = next(d["model"]["expr"] for d in rule["data"] if d.get("datasourceUid") == "prometheus")
    assert "prometheus_tsdb_too_old_samples_total" in expr
    assert "increase(" in expr
    assert "[10m]" in expr
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}
    assert rule["for"] == "0m"


def test_ensure_renders_scrape_targets_from_telemetry_read_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Prometheus scrape targets derive from the telemetry read URLs, so
    the external-migration form (widened listen host + matching URLs) keeps
    self-scrape working without template edits."""
    home = tmp_path / "home"
    _mark_current(home)
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr("shared.config.settings.observability.lgtm_listen_host", "10.0.0.5")
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://10.0.0.5:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://10.0.0.5:9090"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_grafana_url", "http://10.0.0.5:3003"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_query_url", "http://127.0.0.1:3200"
    )

    _lgtm_native.ensure_lgtm_native(_repo(), home, services=frozenset(_lgtm_native.BACKENDS))

    prometheus = yaml.safe_load(
        (_native_dir(home) / "config/prometheus.yml").read_text(encoding="utf-8")
    )
    assert prometheus["storage"]["tsdb"]["out_of_order_time_window"] == "6h"
    targets = {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }
    assert targets == {
        "prometheus": ["10.0.0.5:9090"],
        "tempo": ["127.0.0.1:3200"],
        "loki": ["10.0.0.5:3100"],
        "grafana": ["10.0.0.5:3003"],
    }


def test_render_configs_validates_loki_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    validated: list[dict[str, object]] = []
    write_if_changed = _lgtm_native._write_if_changed

    def record_validation(config: dict[str, object]) -> None:
        validated.append(config)

    def verify_validation_precedes_write(path: Path, content: str) -> None:
        if path.name == "loki.yaml":
            assert validated
        write_if_changed(path, content)

    monkeypatch.setattr(_lgtm_native, "validate_loki_deploy_config", record_validation)
    monkeypatch.setattr(_lgtm_native, "_write_if_changed", verify_validation_precedes_write)

    _lgtm_native._render_configs(_repo(), tmp_path / "native", tmp_path / "home")

    assert validated


def test_native_step_does_not_touch_an_unmarked_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = ConvergeCtx(
        repo=_repo(),
        ava_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
        services=frozenset(_lgtm_native.BACKENDS),
    )

    def fail_ensure(_repo_path: Path, _home: Path) -> None:
        pytest.fail("unmarked homes must be a no-op")

    monkeypatch.setattr(
        _lgtm_native,
        "ensure_lgtm_native",
        fail_ensure,
    )

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert not ctx.ava_home.exists()


def test_render_provisioning_generates_the_dashboard_from_the_render_path(tmp_path: Path) -> None:
    """Task #3697 S3: ava-ops-main.json in the rendered tree comes from the
    metric-registry render, not from the checkout copy — and its hash is
    recorded in the protection sidecar."""
    repo = tmp_path / "repo"
    source = repo / "deploy/lgtm/config/grafana/provisioning"
    (source / "dashboards").mkdir(parents=True)
    (source / "dashboards/ava-ops-main.json").write_text('{"checkout": true}\n', encoding="utf-8")
    (source / "dashboards/dashboards.yml").write_text("yaml\n", encoding="utf-8")
    (source / "datasources").mkdir()
    (source / "datasources/datasources.yml").write_text("datasource\n", encoding="utf-8")
    native = tmp_path / "native"

    _lgtm_native._render_provisioning(repo, native)

    dest_dir = native / "config/provisioning"
    assert (dest_dir / "dashboards/ava-ops-main.json").read_text(encoding="utf-8") == _STUB_RENDER
    assert (dest_dir / "dashboards/dashboards.yml").read_text(encoding="utf-8") == "yaml\n"
    hashes = json.loads((native / "config/provisioning-hashes.json").read_text(encoding="utf-8"))
    assert "dashboards/ava-ops-main.json" in hashes


def test_render_provisioning_keeps_the_generated_dashboard_without_its_source(
    tmp_path: Path,
) -> None:
    """Deleting the checkout copy must not delete the generated artifact: the
    disappearance cleanup only covers verbatim copies."""
    repo = tmp_path / "repo"
    source = repo / "deploy/lgtm/config/grafana/provisioning"
    (source / "dashboards").mkdir(parents=True)
    checkout_copy = source / "dashboards/ava-ops-main.json"
    checkout_copy.write_text('{"checkout": true}\n', encoding="utf-8")
    native = tmp_path / "native"

    _lgtm_native._render_provisioning(repo, native)
    dest = native / "config/provisioning/dashboards/ava-ops-main.json"
    assert dest.is_file()

    checkout_copy.unlink()
    _lgtm_native._render_provisioning(repo, native)

    assert dest.read_text(encoding="utf-8") == _STUB_RENDER


def test_render_provisioning_rewrites_the_dashboard_only_on_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "deploy/lgtm/config/grafana/provisioning").mkdir(parents=True)
    native = tmp_path / "native"

    _lgtm_native._render_provisioning(repo, native)
    dest = native / "config/provisioning/dashboards/ava-ops-main.json"
    before = dest.stat().st_mtime_ns

    _lgtm_native._render_provisioning(repo, native)

    assert dest.stat().st_mtime_ns == before


def test_render_provisioning_dashboard_failure_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "deploy/lgtm/config/grafana/provisioning").mkdir(parents=True)
    native = tmp_path / "native"
    _lgtm_native._render_provisioning(repo, native)
    dest = native / "config/provisioning/dashboards/ava-ops-main.json"
    before = dest.read_text(encoding="utf-8")

    def broken_render(_repo_only: bool = False) -> tuple[str, tuple[str, ...]]:
        raise RuntimeError("render exploded")

    monkeypatch.setattr(
        "shared.metrics.grafana_dashboard_supply.render_dashboard_json", broken_render
    )
    emitted: list[tuple[object, ...]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((*args, kwargs))

    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    _lgtm_native._render_provisioning(repo, native)

    assert dest.read_text(encoding="utf-8") == before
    assert emitted == [
        (
            "telemetry",
            "lgtm_dashboard_render_failed",
            {"level": "warning", "source": "converge", "attributes": {"error": "render exploded"}},
        )
    ]
    err = capsys.readouterr().err
    assert "keeping the previous file" in err


def test_native_listener_ports_are_independent_of_external_query_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "isolated"
    native = home / "lgtm/native"
    monkeypatch.setattr(settings.observability, "lgtm_listen_host", "127.0.0.1")
    monkeypatch.setattr(settings.observability, "lgtm_grafana_listen_host", "127.0.0.1")
    for name, port in (
        ("loki", 53100),
        ("loki_grpc", 59095),
        ("prometheus", 59090),
        ("grafana", 53003),
    ):
        monkeypatch.setattr(settings.observability, f"lgtm_{name}_port", port)
    monkeypatch.setattr(settings.observability, "telemetry_loki_url", "https://query.example/loki")
    repo = Path(__file__).resolve().parents[2]
    _lgtm_native._render_configs(repo, native, home)
    loki = yaml.safe_load((native / "config/loki.yaml").read_text())
    assert loki["server"]["http_listen_port"] == 53100
    assert loki["server"]["grpc_listen_port"] == 59095
    assert "http_port = 53003" in (native / "config/grafana.ini").read_text()
    assert "GRAFANA_ROOT_URL:-http://localhost:53003}" in (native / "grafana/run.sh").read_text()
    argv = service_argv(home, "prometheus")
    assert "--web.listen-address=127.0.0.1:59090" in argv
    assert backend_urls() == {
        "loki": "http://127.0.0.1:53100",
        "prometheus": "http://127.0.0.1:59090",
        "grafana": "http://127.0.0.1:53003",
    }


def test_matching_versions_from_another_platform_are_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    native = home / "lgtm/native"
    native.mkdir(parents=True)
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "linux_amd64")
    repo = Path(__file__).resolve().parents[2]
    assets = _lgtm_native._load_versions(repo)
    for name, asset in assets.items():
        (native / f"version-{name}").write_text(asset["version"])
        (native / f"platform-{name}").write_text("darwin_arm64")
    downloads: list[str] = []

    def download(name: str, _version: str, asset: dict[str, str], _native: Path) -> None:
        assert "linux" in asset["url"]
        if name != "grafana":
            assert "linux-amd64" in asset["member"]
        downloads.append(name)

    monkeypatch.setattr(_lgtm_native, "_download_and_verify", download)
    _lgtm_native.ensure_lgtm_native(repo, home, services=frozenset(_lgtm_native.BACKENDS))
    assert downloads == list(BACKENDS)


def test_pinned_loki_parser_rejection_blocks_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def reject(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "unknown config field")

    monkeypatch.setattr(_lgtm_native.subprocess, "run", reject)
    with pytest.raises(RuntimeError, match="unknown config field"):
        _REAL_VERIFY_LOKI(tmp_path)
    assert calls == [
        [
            str(tmp_path / "lgtm/native/bin/loki"),
            f"-config.file={tmp_path}/lgtm/native/config/loki.yaml",
            "-verify-config",
        ]
    ]
