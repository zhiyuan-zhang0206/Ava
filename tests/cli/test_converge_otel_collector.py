"""cli.commands._otel_collector — binary install + config generation tests.

No network: the download is monkeypatched; the config render and the
idempotence marker are the logic under test. The data-plane receivers
(issue #46) are rendered against the REAL template so the placeholder and
YAML-indentation contract between template and generator is covered.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from cli.commands import _otel_collector as oc


def _fail_ensure_otel_collector(*_args: object, **_kwargs: object) -> None:
    pytest.fail("must skip")


def test_platform_tag_maps_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned asset tags cover darwin/linux/windows amd64+arm64; anything
    else is unsupported (None → sidecar skipped, agents auto-disable OTLP)."""
    cases = [
        ("Darwin", "arm64", "darwin_arm64"),
        ("Darwin", "x86_64", "darwin_amd64"),
        ("Linux", "x86_64", "linux_amd64"),
        ("Linux", "aarch64", "linux_arm64"),
        ("Windows", "AMD64", "windows_amd64"),
        ("Linux", "i686", None),
        ("Windows", "ARM64", None),
    ]
    for system, machine, expected in cases:
        monkeypatch.setattr(platform, "system", lambda _s=system: _s)
        monkeypatch.setattr(platform, "machine", lambda _m=machine: _m)
        assert oc.platform_tag() == expected


def test_generate_config_bakes_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fan-out endpoints + retention are baked from settings; the template's
    placeholders are all consumed (no dangling $TOKEN)."""
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "ava_home: $AVA_HOME\ntempo: $TEMPO_ENDPOINT\nloki: $LOKI_BASE\nprom: $PROM_BASE\nret: $RETENTION_DAYS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://10.0.0.2:14318"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://10.0.0.2:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://10.0.0.2:9090"
    )
    monkeypatch.setattr("shared.config.settings.observability.trace_retention_days", 7)

    out = oc.generate_config(repo, Path("/home/u/.ava"), roles=None)
    assert "ava_home: /home/u/.ava" in out
    assert "tempo: http://10.0.0.2:14318" in out
    assert "loki: http://10.0.0.2:3100/otlp" in out
    assert "prom: http://10.0.0.2:9090/api/v1/otlp" in out
    assert "ret: 7" in out
    assert "$" not in out


def test_generate_config_two_state_observability_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AVA_OBSERVABILITY_URL set -> the gateway collector's LGTM fan-out points
    at the observatory station; unset -> the per-service settings URLs (loopback
    defaults, locked by test_generate_config_bakes_settings)."""
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "ava_home: $AVA_HOME\ntempo: $TEMPO_ENDPOINT\nloki: $LOKI_BASE\nprom: $PROM_BASE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.observability_url", "http://10.0.0.46"
    )
    # A remote observatory is a split-cluster shape: the relay authenticates
    # with the cluster bearer, so the secret must be set (empty fails closed).
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", "cluster-token")

    out = oc.generate_config(repo, Path("/home/u/.ava"), roles=None)
    # WP4: a remote observatory is reached through the station's ONE
    # bearer-authenticated OTLP ingress, never the direct backend /otlp paths.
    assert "loki: http://10.0.0.46:4318" in out
    assert "prom: http://10.0.0.46:4318" in out
    assert "tempo: http://127.0.0.1:14318" in out

    monkeypatch.setattr("shared.config.settings.observability.observability_url", "")
    out = oc.generate_config(repo, Path("/home/u/.ava"), roles=None)
    assert "loki: http://127.0.0.1:3100/otlp" in out
    assert "prom: http://127.0.0.1:9090/api/v1/otlp" in out


def test_write_config_preserves_user_edited_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-edited config.yaml survives the next converge with a warning —
    the content-hash guard protects user edits (web-sources precedent)."""
    dest_dir = tmp_path / "otel-collector"
    dest_dir.mkdir(parents=True)
    config = dest_dir / "config.yaml"
    rendered = "otelcol: default\n"
    oc._write_config(config, rendered)
    assert config.read_text(encoding="utf-8") == rendered

    config.write_text("otelcol: user-edit\n", encoding="utf-8")
    oc._write_config(config, "otelcol: regenerated\n")

    assert config.read_text(encoding="utf-8") == "otelcol: user-edit\n"
    assert "modified locally" in capsys.readouterr().err


def test_write_config_records_and_accepts_converge_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After converge wrote a config, a re-render of the same content is a
    no-op; a re-render with NEW content replaces it (only user edits are
    protected)."""
    dest_dir = tmp_path / "otel-collector"
    dest_dir.mkdir(parents=True)
    config = dest_dir / "config.yaml"
    oc._write_config(config, "v1\n")
    oc._write_config(config, "v2\n")
    assert config.read_text(encoding="utf-8") == "v2\n"


def test_ensure_skips_download_when_version_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A version-matching binary is kept (no download); the config is still
    regenerated each converge."""
    (tmp_path / "otel-collector").mkdir(parents=True)
    (tmp_path / "otel-collector/otelcol-contrib").write_bytes(b"bin")
    (tmp_path / "otel-collector/version").write_text(oc.OTELCOL_CONTRIB_VERSION, encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "ok: $AVA_HOME\n", encoding="utf-8"
    )

    downloaded: list[str] = []
    monkeypatch.setattr(
        oc,
        "_download_and_verify",
        lambda _tag, _dir: downloaded.append(_tag),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr("shared.config.settings.observability.trace_retention_days", 3)

    oc.ensure_otel_collector(repo, tmp_path, roles=None)
    assert downloaded == []
    assert (tmp_path / "otel-collector/config.yaml").exists()


def test_ensure_downloads_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No binary -> download + verify run; the config is written after."""
    (tmp_path / "otel-collector").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "ok: $AVA_HOME\n", encoding="utf-8"
    )

    downloaded: list[str] = []
    monkeypatch.setattr(
        oc,
        "_download_and_verify",
        lambda tag, _d: downloaded.append(tag),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr("shared.config.settings.observability.trace_retention_days", 3)

    oc.ensure_otel_collector(repo, tmp_path, roles=None)
    assert len(downloaded) == 1
    assert (tmp_path / "otel-collector/config.yaml").exists()


def test_unsupported_platform_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No pinned tag -> warn + skip, never download."""
    monkeypatch.setattr(oc, "platform_tag", lambda: None)
    downloaded: list[str] = []
    monkeypatch.setattr(
        oc,
        "_download_and_verify",
        lambda _t, _d: downloaded.append(_t),  # pyright: ignore[reportUnknownArgumentType]
    )
    oc.ensure_otel_collector(tmp_path / "repo", tmp_path, roles=None)
    assert downloaded == []


def _render_real_template(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str] | None,
    *,
    gateway_url: str = "http://10.0.0.10:8000",
    machine_host: str = "10.0.0.10",
    cluster_secret: str = "cluster-token",  # noqa: S107 — fixture token
    self_metrics_port: int = 8888,
    otlp_enabled: bool = True,
    observability_url: str = "",
) -> dict[str, Any]:
    """Render the shipped template for `roles` and parse it as YAML."""
    monkeypatch.setattr("shared.db.direct_db_url", lambda: "postgresql://ava:abc@10.0.0.2:5433/ava")
    monkeypatch.setattr(
        "shared.config.settings.data_plane.redis_url", "redis://:abc@10.0.0.2:6380/0"
    )
    monkeypatch.setattr("shared.config.settings.data_plane.redis_admin_password", "abc")
    monkeypatch.setattr("shared.config.settings.gateway.gateway_url", gateway_url)
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", cluster_secret)
    monkeypatch.setattr(
        "shared.config.settings.observability.otel_collector_metrics_port", self_metrics_port
    )
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", otlp_enabled)
    monkeypatch.setattr("shared.config.settings.observability.observability_url", observability_url)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: machine_host)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-machine")
    repo = Path(__file__).resolve().parents[2]
    out = oc.generate_config(repo, Path("/home/u/.ava"), roles)
    # No placeholder left unconsumed. (A literal dollar survives on purpose:
    # the network-interface exclusion regexp's end-anchor, written $$ in the
    # template.)
    assert re.search(r"\$[A-Z_]{2,}", out) is None
    parsed: Any = yaml.safe_load(out)
    assert isinstance(parsed, dict)
    return parsed


def test_gateway_config_trace_mirror_rotation_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trace mirror's file exporter rotates by size with bounded backups:
    small segments (64 MiB — the structural bound on the ACTIVE file, since
    the file exporter exposes no time-based rotation) and a bounded number of
    backups; day retention comes from $RETENTION_DAYS (the cluster setting)."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))
    rotation = cfg["exporters"]["file/traces"]["rotation"]
    assert rotation["max_megabytes"] == 64
    assert rotation["max_backups"] == 24
    assert rotation["max_days"] == 3  # from trace_retention_days default


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(frozenset({"gateway"}), id="gateway"),
        pytest.param(frozenset({"agent-runner"}), id="runner"),
    ],
)
def test_gateway_and_runner_exporters_drop_newest_after_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str],
) -> None:
    """Every collector-to-downstream hop fails fast when its queue is full.

    Persistent trace/log queues retain their stable IDs and bounded capacity,
    but never block an OTLP receiver waiting for queue space or downstream
    completion. Each send attempt and the whole retry sequence are bounded;
    retry exhaustion drops the batch through the collector's counted failure
    path. Metrics keep their existing 15-minute retry policy in memory.
    """
    cfg = _render_real_template(monkeypatch, roles)
    exporters = cfg["exporters"]

    for exporter_id in ("otlphttp/tempo", "otlphttp/loki"):
        exporter = exporters[exporter_id]
        assert exporter["timeout"] == "5s"
        assert exporter["sending_queue"] == {
            "enabled": True,
            "queue_size": 5000,
            "storage": "file_storage",
            "block_on_overflow": False,
            "wait_for_result": False,
        }
        assert exporter["retry_on_failure"] == {
            "enabled": True,
            "initial_interval": "5s",
            "max_interval": "30s",
            "max_elapsed_time": "15m",
        }

    metrics_exporter = exporters["otlphttp/prometheus"]
    assert metrics_exporter["timeout"] == "5s"
    assert metrics_exporter["sending_queue"] == {
        "enabled": True,
        "queue_size": 1000,
        "block_on_overflow": False,
        "wait_for_result": False,
    }
    assert metrics_exporter["retry_on_failure"]["max_elapsed_time"] == "15m"

    # Remote backpressure never removes the local durable trace sink.
    assert "file/traces" in exporters
    assert cfg["service"]["pipelines"]["traces"]["exporters"] == [
        "otlphttp/tempo",
        "file/traces",
    ]


def test_gateway_config_scrapes_this_clusters_own_data_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway-capable unit owns Postgres+Redis, so its sidecar carries the
    postgresql + redis receivers, dialed DIRECT (never the pooler) with the
    credentials the cluster's own URLs carry."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))
    receivers = cfg["receivers"]
    assert receivers["postgresql"]["endpoint"] == "10.0.0.2:5433"
    assert receivers["postgresql"]["username"] == "ava"
    assert receivers["postgresql"]["password"] == "abc"  # noqa: S105 — fixture value
    assert receivers["postgresql"]["databases"] == ["ava"]
    assert receivers["redis"]["endpoint"] == "10.0.0.2:6380"
    assert receivers["redis"]["password"] == "abc"  # noqa: S105 — fixture value
    assert receivers["redis"]["password"] != "cluster-token"  # noqa: S105 — fixture token
    infra = cfg["service"]["pipelines"]["metrics/infra"]
    assert infra["receivers"] == [
        "host_metrics",
        "prometheus/otelcol",
        "postgresql",
        "redis",
    ]


def test_gateway_config_skips_postgres_receiver_when_otlp_export_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling OTLP export keeps Redis metrics but prevents PostgreSQL receiver startup."""
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        otlp_enabled=False,
    )

    assert "postgresql" not in cfg["receivers"]
    assert "redis" in cfg["receivers"]
    assert "postgresql" not in cfg["service"]["pipelines"]["metrics/infra"]["receivers"]


def test_gateway_config_skips_postgres_receiver_without_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contrib postgresql receiver rejects an empty password, while the
    redis receiver supports the no-auth single-box posture."""
    monkeypatch.setattr("shared.db.direct_db_url", lambda: "postgresql://ava@10.0.0.2:5433/ava")
    monkeypatch.setattr("shared.config.settings.data_plane.redis_url", "redis://10.0.0.2:6380/0")
    monkeypatch.setattr("shared.config.settings.gateway.gateway_url", "http://localhost:8000")
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", "")
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "localhost")
    repo = Path(__file__).resolve().parents[2]

    cfg = yaml.safe_load(
        oc.generate_config(repo, Path("/home/u/.ava"), frozenset({"gateway", "agent-runner"}))
    )

    assert "postgresql" not in cfg["receivers"]
    assert cfg["receivers"]["redis"]["endpoint"] == "10.0.0.2:6380"
    assert cfg["service"]["pipelines"]["metrics/infra"]["receivers"] == [
        "host_metrics",
        "prometheus/otelcol",
        "redis",
    ]


def test_runner_config_has_host_metrics_but_no_data_plane_receivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure agent-runner's DB/Redis URLs point at the GATEWAY's data plane.
    Scraping from there would duplicate the gateway's own series under a
    second `host` / `machine_name` identity, so the two receivers are omitted
    entirely — host metrics, which ARE this machine's, stay."""
    cfg = _render_real_template(monkeypatch, frozenset({"agent-runner"}))
    assert "postgresql" not in cfg["receivers"]
    assert "redis" not in cfg["receivers"]
    assert "host_metrics" in cfg["receivers"]
    assert cfg["service"]["pipelines"]["metrics/infra"]["receivers"] == [
        "host_metrics",
        "prometheus/otelcol",
    ]


def test_unconfigured_unit_has_no_data_plane_receivers(monkeypatch: pytest.MonkeyPatch) -> None:
    """roles=None is a unit converge has not configured yet — there are no
    cluster URLs to read, so nothing data-plane is rendered."""
    cfg = _render_real_template(monkeypatch, None)
    assert "postgresql" not in cfg["receivers"]
    assert "redis" not in cfg["receivers"]


def test_infra_metrics_ride_their_own_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """App metrics arrive over OTLP already carrying machine/agent_id
    attributes and must not be relabelled; the host-identity processors
    therefore sit on the infra pipeline only."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))
    pipelines = cfg["service"]["pipelines"]
    assert pipelines["metrics"]["receivers"] == ["otlp", "otlp/remote"]
    assert "transform/host_label" not in pipelines["metrics"]["processors"]
    assert "transform/host_label" in pipelines["metrics/infra"]["processors"]
    assert "resource_detection/host" in pipelines["metrics/infra"]["processors"]


def test_infra_pipeline_stamps_machine_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Infra datapoints keep physical host identity and gain Ava roster identity."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))
    statements = cfg["processors"]["transform/host_label"]["metric_statements"][0]["statements"]
    assert 'set(attributes["host"], resource.attributes["host.name"])' in statements
    assert 'set(attributes["machine_name"], "test-machine")' in statements


def test_collector_self_metrics_reader_is_per_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each unit binds and scrapes its own configurable collector metrics port."""
    default_cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))
    default_reader = default_cfg["service"]["telemetry"]["metrics"]["readers"][0]["pull"][
        "exporter"
    ]["prometheus"]
    assert default_reader == {"host": "localhost", "port": 8888}

    override_cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        self_metrics_port=8889,
    )
    override_reader = override_cfg["service"]["telemetry"]["metrics"]["readers"][0]["pull"][
        "exporter"
    ]["prometheus"]
    assert override_reader == {"host": "localhost", "port": 8889}
    override_scrape = override_cfg["receivers"]["prometheus/otelcol"]["config"]["scrape_configs"]
    assert override_scrape[0]["static_configs"] == [{"targets": ["localhost:8889"]}]


def test_collector_internal_logs_are_warn_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exporter retry notices stay out of service stdout while warnings remain."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))
    assert cfg["service"]["telemetry"]["logs"]["level"] == "warn"


def test_logs_merge_event_and_filelog_transforms_before_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The logs pipeline promotes bounded event dimensions and labels tailed
    session files before the final batch processor."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))

    processor = cfg["processors"]["transform/promote_event_labels"]
    assert processor["error_mode"] == "ignore"
    assert processor["log_statements"] == [
        {
            "context": "log",
            "statements": [
                'set(resource.attributes["agent_id"], attributes["agent_id"]) where attributes["agent_id"] != nil',
                'set(resource.attributes["event_name"], attributes["event_name"]) where attributes["event_name"] != nil',
            ],
        }
    ]
    filelog_processor = cfg["processors"]["transform/filelog_service"]
    assert filelog_processor["log_statements"] == [
        {
            "context": "log",
            "conditions": ['attributes["log.file.name"] != nil'],
            "statements": [
                'set(attributes["tmp_svc"], attributes["log.file.name"])',
                'replace_pattern(attributes["tmp_svc"], "\\\\.out\\\\.log$", "")',
                'replace_pattern(attributes["tmp_svc"], "^(updater|rollout)-[0-9]+$", "$1")',
                'set(resource.attributes["service.name"], attributes["tmp_svc"])',
                'delete_key(attributes, "tmp_svc")',
            ],
        }
    ]
    assert cfg["extensions"]["file_storage/logoffsets"] == {
        "directory": "/home/u/.ava/otel-collector/log-offsets",
        "create_directory": True,
    }
    logs = cfg["service"]["pipelines"]["logs"]
    assert logs["receivers"] == [
        "otlp",
        "otlp/remote",
        "filelog/sessions",
        "filelog/services",
        "filelog/orchestration",
    ]
    assert logs["processors"] == [
        "memory_limiter",
        "filter/cluster_allow",
        "transform/promote_event_labels",
        "transform/filelog_service",
        "batch",
    ]


def test_local_otlp_pipelines_drop_mismatched_cluster_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))

    processor = cfg["processors"]["filter/cluster_allow"]
    expected = [
        'resource.attributes["cluster"] != nil and resource.attributes["cluster"] != ".ava"'
    ]
    assert processor == {
        "error_mode": "ignore",
        "traces": {"span": expected},
        "metrics": {"metric": expected},
        "logs": {"log_record": expected},
    }
    pipelines = cfg["service"]["pipelines"]
    for name in ("logs", "metrics", "traces"):
        assert pipelines[name]["processors"][:2] == ["memory_limiter", "filter/cluster_allow"]
    assert "filter/cluster_allow" not in pipelines["metrics/infra"]["processors"]
    assert "filter/cluster_allow" not in pipelines["traces/remote"]["processors"]


def test_non_lgtm_gateway_converge_skips_collector_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[2],
        ava_home=home,
        roles=frozenset({"gateway", "agent-runner"}),
    )

    def must_not_install(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a non-LGTM gateway must not install a collector")

    monkeypatch.setattr(oc, "ensure_otel_collector", must_not_install)

    oc.ensure_otel_collector_step(ctx)

    assert not (home / "otel-collector").exists()
    err = capsys.readouterr().err
    assert "gateway" in err
    assert "lgtm-host" in err
    assert "collector skipped" in err


def test_non_lgtm_gateway_with_explicit_endpoint_installs_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit AVA_TELEMETRY_OTLP_ENDPOINT override opens the converge
    step on a non-LGTM gateway: the operator opted into explicit export, so
    the local sidecar is installed instead of skipped/reaped."""
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[2],
        ava_home=home,
        roles=frozenset({"gateway"}),
    )

    installed: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        oc,
        "ensure_otel_collector",
        lambda *args: installed.append(args),  # pyright: ignore[reportUnknownArgumentType]
    )

    oc.ensure_otel_collector_step(ctx)

    assert len(installed) == 1
    assert installed[0][1] == home


def test_non_lgtm_gateway_reaps_orphan_collector_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[2],
        ava_home=home,
        roles=frozenset({"gateway"}),
    )
    killed: list[str] = []
    expected_flags: list[bool] = []

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)

    def _collector_session_exists(session: str) -> bool:
        return session == "ava-otel-collector"

    monkeypatch.setattr("cli.commands._has_session", _collector_session_exists)

    def _record_kill(session: str, *, expected: bool = False) -> tuple[bool, str]:
        killed.append(session)
        expected_flags.append(expected)
        return True, "graceful"

    monkeypatch.setattr("cli.commands._session_lifecycle._graceful_kill_session", _record_kill)

    oc.ensure_otel_collector_step(ctx)

    assert killed == ["ava-otel-collector"]
    assert expected_flags == [True]
    assert "reaped orphan session ava-otel-collector" in capsys.readouterr().err


def test_non_lgtm_gateway_without_session_skips_reap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[2],
        ava_home=home,
        roles=frozenset({"gateway"}),
    )
    killed: list[str] = []

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)

    def _no_session(_session: str) -> bool:
        return False

    monkeypatch.setattr("cli.commands._has_session", _no_session)

    def _record_kill(session: str, *, expected: bool = False) -> tuple[bool, str]:
        killed.append(session)
        return True, "graceful"

    monkeypatch.setattr("cli.commands._session_lifecycle._graceful_kill_session", _record_kill)

    oc.ensure_otel_collector_step(ctx)

    assert killed == []
    assert "reaped orphan session" not in capsys.readouterr().err


def test_non_lgtm_gateway_reports_and_preserves_residual_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".ava-preview"
    collector = home / "otel-collector"
    collector.mkdir(parents=True)
    config = collector / "config.yaml"
    config.write_text("stale: true\n", encoding="utf-8")
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[2],
        ava_home=home,
        roles=frozenset({"gateway"}),
    )

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)
    oc.ensure_otel_collector_step(ctx)

    assert config.read_text(encoding="utf-8") == "stale: true\n"
    assert "stale/residual" in capsys.readouterr().err


def test_session_filelog_receivers_are_disjoint_and_bound_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell transcripts and service output use disjoint file sets.

    Agent main logs begin with identical telemetry banners, so admitting them
    to either receiver would restore the fingerprint-collision re-watch storm.
    """
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))

    sessions = cfg["receivers"]["filelog/sessions"]
    assert sessions == {
        "include": ["/home/u/.ava/logs/ava-agent-*-shell-*.out.log"],
        "start_at": "end",
        "include_file_name": True,
        "include_file_path": False,
        "storage": "file_storage/logoffsets",
        "poll_interval": "10s",
        "polls_to_archive": 50,
        "max_concurrent_files": 200,
    }

    services = cfg["receivers"]["filelog/services"]
    assert services == {
        "include": ["/home/u/.ava/logs/*.out.log"],
        "exclude": [
            "/home/u/.ava/logs/ava-agent-*.out.log",
            "/home/u/.ava/logs/ava-otel-collector.out.log",
        ],
        "start_at": "end",
        "include_file_name": True,
        "include_file_path": False,
        "storage": "file_storage/logoffsets",
        "poll_interval": "10s",
        "polls_to_archive": 50,
        "max_concurrent_files": 200,
    }


def test_runner_forwards_to_authenticated_gateway_ingress_without_renaming_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure runner relays all signals to the gateway collector, never to
    loopback backends. Exporter IDs stay byte-for-byte stable so a converge
    adopts the existing file_storage queues instead of orphaning their backlog."""
    cfg = _render_real_template(monkeypatch, frozenset({"agent-runner"}))

    assert set(cfg["receivers"]) >= {"otlp", "host_metrics", "prometheus/otelcol"}
    assert "otlp/remote" not in cfg["receivers"]
    exporters = cfg["exporters"]
    expected_ids = {"otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus", "file/traces"}
    assert set(exporters) == expected_ids
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        exporter = exporters[exporter_id]
        assert exporter["endpoint"] == "http://10.0.0.10:4318"
        assert exporter["headers"] == {"Authorization": "Bearer cluster-token"}
    assert exporters["otlphttp/tempo"]["sending_queue"]["storage"] == "file_storage"
    assert exporters["otlphttp/loki"]["sending_queue"]["storage"] == "file_storage"
    assert "storage" not in exporters["otlphttp/prometheus"]["sending_queue"]
    rendered = str(cfg)
    assert "127.0.0.1:14318" not in rendered
    assert "127.0.0.1:3100" not in rendered
    assert "127.0.0.1:9090" not in rendered


def test_gateway_has_separate_authenticated_reachable_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A split gateway keeps local producers on loopback/no-auth and accepts
    remote relays only on its declared reachable address with bearer auth."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))

    assert cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4318"}
    assert cfg["receivers"]["otlp/remote"]["protocols"]["http"] == {
        "endpoint": "10.0.0.10:4318",
        "auth": {"authenticator": "bearertokenauth/cluster"},
    }
    assert cfg["extensions"]["bearertokenauth/cluster"] == {"token": "cluster-token"}
    assert "bearertokenauth/cluster" in cfg["service"]["extensions"]
    # Remote traces fan out to Tempo but never enter the gateway's local mirror.
    assert cfg["service"]["pipelines"]["traces/remote"]["exporters"] == ["otlphttp/tempo"]
    assert cfg["service"]["pipelines"]["traces"]["exporters"] == [
        "otlphttp/tempo",
        "file/traces",
    ]
    for exporter_id, endpoint in (
        ("otlphttp/tempo", "http://127.0.0.1:14318"),
        ("otlphttp/loki", "http://127.0.0.1:3100/otlp"),
        ("otlphttp/prometheus", "http://127.0.0.1:9090/api/v1/otlp"),
    ):
        assert cfg["exporters"][exporter_id]["endpoint"] == endpoint


def test_station_has_separate_authenticated_reachable_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure observability-station exposes the same bearer-authenticated
    remote OTLP ingress a gateway does — the surface remote gateway
    collectors relay to (WP4, task #1946)."""
    cfg = _render_real_template(monkeypatch, frozenset({"observability-station"}))

    assert cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4318"}
    assert cfg["receivers"]["otlp/remote"]["protocols"]["http"] == {
        "endpoint": "10.0.0.10:4318",
        "auth": {"authenticator": "bearertokenauth/cluster"},
    }
    assert cfg["extensions"]["bearertokenauth/cluster"] == {"token": "cluster-token"}
    assert "bearertokenauth/cluster" in cfg["service"]["extensions"]
    # Remote traces fan out to the station's own Tempo and never enter the
    # station's local trace mirror; remote logs/metrics land in its Loki/Prom.
    assert cfg["service"]["pipelines"]["traces/remote"]["exporters"] == ["otlphttp/tempo"]
    assert cfg["service"]["pipelines"]["traces"]["exporters"] == [
        "otlphttp/tempo",
        "file/traces",
    ]
    assert "otlp/remote" in cfg["service"]["pipelines"]["logs"]["receivers"]
    assert "otlp/remote" in cfg["service"]["pipelines"]["metrics"]["receivers"]
    for exporter_id, endpoint in (
        ("otlphttp/tempo", "http://127.0.0.1:14318"),
        ("otlphttp/loki", "http://127.0.0.1:3100/otlp"),
        ("otlphttp/prometheus", "http://127.0.0.1:9090/api/v1/otlp"),
    ):
        assert cfg["exporters"][exporter_id]["endpoint"] == endpoint


def test_remote_observatory_gateway_relays_to_station_single_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway consuming a remote observatory (AVA_OBSERVABILITY_URL set)
    relays every signal to the station's single bearer-authenticated OTLP
    ingress with the cluster bearer — it never dials the station's
    loopback-bound backends directly (WP4, task #1946)."""
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        observability_url="http://10.0.0.46",
    )
    exporters = cfg["exporters"]
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        exporter = exporters[exporter_id]
        assert exporter["endpoint"] == "http://10.0.0.46:4318"
        assert exporter["headers"] == {"Authorization": "Bearer cluster-token"}
    rendered = str(cfg)
    assert "127.0.0.1:3100" not in rendered
    assert "127.0.0.1:9090" not in rendered
    assert "127.0.0.1:14318" not in rendered
    # The gateway keeps its local trace mirror and its local loopback receiver.
    assert "file/traces" in exporters
    assert cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4318"}


def test_remote_observatory_relay_without_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote observatory with no cluster secret cannot authenticate the
    relay — converge must fail, not ship an unauthenticated fan-out."""
    with pytest.raises(RuntimeError, match="cluster secret"):
        _render_real_template(
            monkeypatch,
            frozenset({"gateway", "agent-runner"}),
            cluster_secret="",
            observability_url="http://10.0.0.46",
        )


def test_station_otlp_ingress_port_follows_single_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The station's advertised unit url (shared.machines.unit_dial_url) and
    its remote receiver bind the SAME port — AVA_TELEMETRY_OTLP_PORT is the
    single knob for both (WP4, task #1946)."""
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_port", 4321)
    cfg = _render_real_template(monkeypatch, frozenset({"observability-station"}))
    assert cfg["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"] == ("10.0.0.10:4321")
    from shared.machines import unit_dial_url

    assert unit_dial_url(frozenset({"observability-station"})) == "http://10.0.0.10:4321"


def test_otlp_ingress_port_single_source_renders_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVA_TELEMETRY_OTLP_PORT is the one knob for the OTLP ingress: the
    sidecar receiver, the gateway's authenticated remote receiver, and the
    pure-runner relay endpoint all follow it (WP3, task #1945). Under the
    default 4318 the rendered endpoints are byte-identical to the historical
    values (locked by the tests above)."""
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_port", 4319)
    gateway_cfg = _render_real_template(monkeypatch, frozenset({"gateway"}))
    assert gateway_cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4319"}
    assert gateway_cfg["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"] == (
        "10.0.0.10:4319"
    )
    runner_cfg = _render_real_template(monkeypatch, frozenset({"agent-runner"}))
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert runner_cfg["exporters"][exporter_id]["endpoint"] == "http://10.0.0.10:4319"


def test_hybrid_gateway_runner_still_serves_remote_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway capability plus a non-empty secret is the cross-machine posture
    even when the same host also runs agents (the production Mac mini shape)."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))
    remote = cfg["receivers"]["otlp/remote"]["protocols"]["http"]
    assert remote["endpoint"] == "10.0.0.10:4318"
    assert remote["auth"] == {"authenticator": "bearertokenauth/cluster"}


def test_gateway_ipv6_receiver_uses_unambiguous_bracketed_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        gateway_url="http://[fd7a:115c:a1e0::10]:8000",
        machine_host="fd7a:115c:a1e0::10",
    )
    assert (
        cfg["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"]
        == "[fd7a:115c:a1e0::10]:4318"
    )


def test_runner_ipv6_gateway_url_uses_unambiguous_bracketed_exporter_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"agent-runner"}),
        gateway_url="http://[fd7a:115c:a1e0::10]:8000",
        machine_host="fd7a:115c:a1e0::20",
    )
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert cfg["exporters"][exporter_id]["endpoint"] == "http://[fd7a:115c:a1e0::10]:4318"


def test_single_box_keeps_every_otlp_listener_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zero-config combined role has no cross-machine ingress or secret."""
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        gateway_url="http://localhost:8000",
        machine_host="localhost",
        cluster_secret="",
    )
    assert "otlp/remote" not in cfg["receivers"]
    assert "bearertokenauth/cluster" not in cfg["extensions"]
    assert cfg["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4318"
    assert all(
        exporter["endpoint"].startswith("http://127.0.0.1:")
        for name, exporter in cfg["exporters"].items()
        if name.startswith("otlphttp/")
    )


def test_secret_set_single_box_still_collapses_remote_ingress_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret can be enabled on a combined single box. Its loopback machine
    host still means there are no remote runners, so converge must not invent
    a reachable receiver or fail the otherwise-valid local topology."""
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        gateway_url="http://localhost:8000",
        machine_host="localhost",
        cluster_secret="cluster-token",  # noqa: S106 — fixture token
    )
    assert "otlp/remote" not in cfg["receivers"]
    assert "bearertokenauth/cluster" not in cfg["extensions"]
    assert cfg["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4318"


def test_pure_role_units_collapse_remote_ingress_without_remote_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA #1156 NIT-1: the converge guard matches the registration guard. A
    pure gateway or pure station with an EMPTY secret or a LOOPBACK reachable
    host has no remote peers (single-box posture) — no receiver is rendered
    and converge does not fail, exactly like the combined single-box unit.
    Only a wildcard host (a malformed identity) fails closed."""
    for roles in (frozenset({"gateway"}), frozenset({"observability-station"})):
        no_secret = _render_real_template(
            monkeypatch,
            roles,
            cluster_secret="",
            machine_host="10.0.0.10",
        )
        assert "otlp/remote" not in no_secret["receivers"]
        assert "bearertokenauth/cluster" not in no_secret["extensions"]

        loopback = _render_real_template(
            monkeypatch,
            roles,
            cluster_secret="cluster-token",  # noqa: S106 — fixture token
            machine_host="localhost",
        )
        assert "otlp/remote" not in loopback["receivers"]
        assert "bearertokenauth/cluster" not in loopback["extensions"]


@pytest.mark.parametrize(
    ("roles", "gateway_url", "machine_host", "cluster_secret", "message"),
    [
        (
            frozenset({"agent-runner"}),
            "http://localhost:8000",
            "10.0.0.20",
            "token",
            "gateway URL",
        ),
        (frozenset({"agent-runner"}), "http://0.0.0.0:8000", "10.0.0.20", "token", "gateway URL"),
        (frozenset({"agent-runner"}), "http://[::]:8000", "10.0.0.20", "token", "gateway URL"),
        (
            frozenset({"agent-runner"}),
            "http://10.0.0.10:8000",
            "10.0.0.20",
            "",
            "cluster secret",
        ),
        (frozenset({"gateway"}), "http://10.0.0.10:8000", "0.0.0.0", "token", "reachable host"),  # noqa: S104 — rejection fixture
        (
            frozenset({"gateway"}),
            "http://[fd7a:115c:a1e0::10]:8000",
            "::",
            "token",
            "reachable host",
        ),
        # WP4: a pure observability-station fails closed on a wildcard host
        # exactly like a pure gateway. Empty secret / loopback host are the
        # single-box posture and render NO remote receiver instead (QA #1156
        # NIT-1 — the converge guard now matches the registration guard's
        # "legal when nothing remote dials it" rule; see
        # test_single_box_*_collapses_remote_ingress and the no_remote
        # assertions in test_station_has_separate_authenticated_reachable_receiver).
        (
            frozenset({"observability-station"}),
            "http://10.0.0.10:8000",
            "0.0.0.0",  # noqa: S104 — rejection fixture
            "token",
            "reachable host",
        ),
    ],
)
def test_split_topology_fails_closed_when_ingress_identity_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str],
    gateway_url: str,
    machine_host: str,
    cluster_secret: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _render_real_template(
            monkeypatch,
            roles,
            gateway_url=gateway_url,
            machine_host=machine_host,
            cluster_secret=cluster_secret,
        )


def test_collector_self_metrics_are_scraped_for_queue_and_drop_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector's own queue depth/capacity/enqueue-failure counters ride
    the infra pipeline, so a recovered path carries evidence of the outage."""
    cfg = _render_real_template(monkeypatch, frozenset({"agent-runner"}))
    scrape = cfg["receivers"]["prometheus/otelcol"]["config"]["scrape_configs"]
    assert scrape == [
        {
            "job_name": "ava-otel-collector",
            "scrape_interval": "30s",
            "static_configs": [{"targets": ["localhost:8888"]}],
        }
    ]


def test_config_file_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every split role's config carries the cluster bearer, and a gateway
    also carries pg/redis credentials. The file is 0600 from first creation,
    not write-as-0644 followed by chmod."""
    if platform.system() == "Windows":
        pytest.skip("POSIX file modes only")
    (tmp_path / "otel-collector").mkdir(parents=True)
    (tmp_path / "otel-collector/otelcol-contrib").write_bytes(b"bin")
    (tmp_path / "otel-collector/version").write_text(oc.OTELCOL_CONTRIB_VERSION, encoding="utf-8")
    monkeypatch.setattr(oc, "_download_and_verify", lambda _tag, _dir: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.db.direct_db_url", lambda: "postgresql://ava:abc@10.0.0.2:5433/ava")
    monkeypatch.setattr(
        "shared.config.settings.data_plane.redis_url", "redis://:abc@10.0.0.2:6380/0"
    )
    monkeypatch.setattr("shared.config.settings.gateway.gateway_url", "http://10.0.0.10:8000")
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", "cluster-token")
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.10")
    repo = Path(__file__).resolve().parents[2]

    replace = Path.replace
    modes_before_publish: list[int] = []

    def _record_replace(source: Path, target: Path) -> Path:
        modes_before_publish.append(source.stat().st_mode & 0o777)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", _record_replace)
    oc.ensure_otel_collector(repo, tmp_path, frozenset({"agent-runner"}))
    config = tmp_path / "otel-collector/config.yaml"
    assert modes_before_publish == [0o600]
    assert config.stat().st_mode & 0o777 == 0o600
    assert "Bearer cluster-token" in config.read_text(encoding="utf-8")


# -- issue #172: bounded, loud download -------------------------------------


class _ChunkedResp:
    """A urlopen response stand-in that yields chunk by chunk."""

    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self._chunks = list(chunks)
        self._headers = headers or {}

    def __enter__(self) -> _ChunkedResp:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    @property
    def headers(self) -> dict[str, str]:
        return self._headers


def test_stream_download_writes_all_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The streamed bytes land in the tarball path; the final line reports the
    size; no heartbeat fires for a fast download."""
    payload = b"x" * (1 << 20)  # 1 MiB

    def _fake_urlopen(_url: str, **kw: object) -> _ChunkedResp:
        return _ChunkedResp([payload] * 4, {"Content-Length": str(4 << 20)})

    monkeypatch.setattr(oc.urllib.request, "urlopen", _fake_urlopen)
    dest = tmp_path / "t.tar.gz"

    oc._stream_download("https://example.invalid/t.tar.gz", dest)

    assert dest.read_bytes() == payload * 4
    out = capsys.readouterr().out
    assert "downloaded 4.2 MB" in out
    assert "in 0s" in out


def test_stream_download_honors_socket_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The socket timeout is passed through to urlopen — the per-read guard
    against a wedged connection."""
    seen: dict[str, object] = {}

    def _fake_urlopen(url: str, timeout: float) -> None:
        seen["timeout"] = timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(oc.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(TimeoutError):
        oc._stream_download("https://example.invalid/t.tar.gz", tmp_path / "t.tar.gz")
    assert seen["timeout"] == oc._DOWNLOAD_SOCKET_TIMEOUT_S


def test_download_with_retry_exhausts_and_names_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All attempts fail -> RuntimeError naming the URL; each attempt is
    announced, and the retry sleeps between attempts."""
    calls = {"n": 0}

    def _always_fail(_url: str, _dest: Path) -> None:
        calls["n"] += 1
        raise OSError("connection reset")

    monkeypatch.setattr(oc, "_stream_download", _always_fail)
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]  # no real backoff wait

    with pytest.raises(RuntimeError) as ei:
        oc._download_with_retry("https://example.invalid/t.tar.gz", tmp_path / "t.tar.gz")

    assert calls["n"] == oc._DOWNLOAD_ATTEMPTS
    assert "https://example.invalid/t.tar.gz" in str(ei.value)
    assert "after 3 attempts" in str(ei.value)
    err = capsys.readouterr().err
    assert "attempt 1/3" in err and "attempt 3/3" in err


def test_download_with_retry_succeeds_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient first failure is retried and the second attempt wins."""
    calls = {"n": 0}

    def _fail_then_win(_url: str, dest: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection reset")
        dest.write_bytes(b"ok")

    monkeypatch.setattr(oc, "_stream_download", _fail_then_win)
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    oc._download_with_retry("https://example.invalid/t.tar.gz", tmp_path / "t.tar.gz")
    assert calls["n"] == 2
    assert (tmp_path / "t.tar.gz").read_bytes() == b"ok"
