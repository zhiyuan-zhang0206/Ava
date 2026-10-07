"""cli.commands.observability.otel_collector — binary install + config generation tests.

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
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import yaml

from base.deploy.release import collector_artifact as artifact
from cli.commands.observability import otel_collector as oc


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
        assert artifact.platform_tag() == expected


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
        "base.config.settings.observability.telemetry_tempo_endpoint", "http://10.0.0.2:14318"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_loki_url", "http://10.0.0.2:3100"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_prometheus_url", "http://10.0.0.2:9090"
    )
    monkeypatch.setattr("base.config.settings.observability.trace_retention_days", 7)

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
        "base.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr("base.config.settings.observability.observability_url", "http://10.0.0.46")
    # A remote observatory is a split-cluster shape: the relay authenticates
    # with the cluster bearer, so the secret must be set (empty fails closed).
    monkeypatch.setattr("base.config.settings.data_plane.cluster_secret", "cluster-token")

    out = oc.generate_config(repo, Path("/home/u/.ava"), roles=None)
    # WP4: a remote observatory is reached through the station's ONE
    # bearer-authenticated OTLP ingress, never the direct backend /otlp paths.
    assert "loki: http://10.0.0.46:4318" in out
    assert "prom: http://10.0.0.46:4318" in out
    assert "tempo: http://127.0.0.1:14318" in out

    monkeypatch.setattr("base.config.settings.observability.observability_url", "")
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
    (tmp_path / "otel-collector/version").write_text(
        artifact.OTELCOL_CONTRIB_VERSION, encoding="utf-8"
    )
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "ok: $AVA_HOME\n", encoding="utf-8"
    )

    downloaded: list[str] = []
    monkeypatch.setattr(
        artifact,
        "download_and_verify",
        lambda _tag, _dir: downloaded.append(_tag),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr("base.config.settings.observability.trace_retention_days", 3)

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
        artifact,
        "download_and_verify",
        lambda tag, _d: downloaded.append(tag),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        "base.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr("base.config.settings.observability.trace_retention_days", 3)

    oc.ensure_otel_collector(repo, tmp_path, roles=None)
    assert len(downloaded) == 1
    assert (tmp_path / "otel-collector/config.yaml").exists()


def test_unsupported_platform_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No pinned tag -> warn + skip, never download."""
    monkeypatch.setattr(artifact, "platform_tag", lambda: None)
    downloaded: list[str] = []
    monkeypatch.setattr(
        artifact,
        "download_and_verify",
        lambda _t, _d: downloaded.append(_t),  # pyright: ignore[reportUnknownArgumentType]
    )
    oc.ensure_otel_collector(tmp_path / "repo", tmp_path, roles=None)
    assert downloaded == []


_HOME = Path("/home/u/.ava")
_PG_PORT = 5433
# The gateway login a start process adopts into AVA_DB_URL (port 1: never dialed).
_DELIVERED = "postgresql://ava_g3_gateway:generation-login-password@127.0.0.1:1/ava"


def _local_data_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback Redis and this home's registry record (the DB URL stays the caller's)."""
    from base import cluster

    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"postgres": _PG_PORT, "redis": 6380}),
        gateway_home=str(_HOME),
        created_at="test",
    )

    def _get_record(home: Path) -> cluster.ClusterRecord | None:
        return record if home == _HOME else None

    monkeypatch.setattr(cluster, "get_record", _get_record)
    monkeypatch.setattr(
        "base.config.settings.data_plane.redis_url", "redis://ava:runtime@127.0.0.1:6380/0"
    )
    monkeypatch.setattr("base.config.settings.data_plane.redis_admin_password", "abc")


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
    _local_data_plane(monkeypatch)
    monkeypatch.setattr("base.config.settings.gateway.gateway_url", gateway_url)
    from base.host.net.url_secret import url_with_host

    monkeypatch.setattr(
        "base.config.settings.observability.gateway_otlp_endpoint",
        url_with_host("http://localhost:4318", urlsplit(gateway_url).hostname or ""),
    )
    monkeypatch.setattr("base.config.settings.data_plane.cluster_secret", cluster_secret)
    monkeypatch.setattr(
        "base.config.settings.observability.otel_collector_metrics_port", self_metrics_port
    )
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", otlp_enabled)
    monkeypatch.setattr("base.config.settings.observability.observability_url", observability_url)
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: machine_host)
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "test-machine")
    repo = Path(__file__).resolve().parents[4]
    out = oc.generate_config(repo, _HOME, roles)
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
        pytest.param(frozenset({"observability-station"}), id="station"),
    ],
)
def test_file_storage_caps_queue_bytes_in_every_shape(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str],
) -> None:
    """The persistent sending queues carry a byte cap in every shape.

    A request-count bound alone (5,000) still lets sustained backpressure grow
    each queue file without limit; 1 GiB is the calibrated fleet-wide value
    (task #4012) — the measured steady state peaks at 580M against a 30G
    minimum disk. At the cap the collector rejects the newest write, the same
    counted loss path as a request-full queue.
    """
    cfg = _render_real_template(monkeypatch, roles)
    assert cfg["extensions"]["file_storage"] == {
        "directory": "/home/u/.ava/otel-collector/queue",
        "create_directory": True,
        "timeout": "1s",
        "max_size": 1073741824,
    }


def test_gateway_config_scrapes_this_clusters_own_data_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway-capable unit owns Postgres+Redis, so its sidecar carries the
    postgresql + redis receivers. Postgres is dialed DIRECT (never the pooler)
    over the home's owner-only socket as the password-less monitoring role,
    never as the write-generation login the start process adopted; Redis with its admin password."""
    from base.cluster.authority import MONITOR_ROLE
    from base.db.pg_admin import pg_socket_path

    monkeypatch.setattr("base.config.settings.data_plane.db_url", _DELIVERED)
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))
    rendered = yaml.safe_dump(cfg)
    assert "generation-login-password" not in rendered and "ava_g3_gateway" not in rendered
    receivers = cfg["receivers"]
    socket_dir = pg_socket_path(_HOME).as_posix()
    # The receiver prefixes a unix endpoint's host with "/" itself.
    assert receivers["postgresql"]["endpoint"] == f"{socket_dir.lstrip('/')}:{_PG_PORT}"
    assert receivers["postgresql"]["transport"] == "unix"
    assert receivers["postgresql"]["username"] == MONITOR_ROLE
    assert receivers["postgresql"]["password"] == oc._PEER_PLACEHOLDER
    assert receivers["postgresql"]["databases"] == ["ava"]
    assert receivers["redis"]["endpoint"] == "127.0.0.1:6380"
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


def test_remote_managed_plane_omits_the_postgres_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-managed plane has no owner-only socket or monitoring role on
    this host (its provider monitors it), so only Redis is scraped; the
    provider's database credential never reaches the config."""
    monkeypatch.setattr(
        "base.config.settings.data_plane.db_url",
        "postgresql://ava:provider-password@10.0.0.2:5433/ava",
    )
    monkeypatch.setattr("base.config.settings.data_plane.redis_url", "redis://10.0.0.2:6380/0")
    monkeypatch.setattr("base.config.settings.data_plane.redis_admin_password", "abc")
    monkeypatch.setattr("base.config.settings.gateway.gateway_url", "http://localhost:8000")
    monkeypatch.setattr("base.config.settings.data_plane.cluster_secret", "")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "localhost")
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "test-machine")
    repo = Path(__file__).resolve().parents[4]

    rendered = oc.generate_config(repo, _HOME, frozenset({"gateway", "agent-runner"}))
    cfg = yaml.safe_load(rendered)

    assert "provider-password" not in rendered
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
        repo=Path(__file__).resolve().parents[4],
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
        repo=Path(__file__).resolve().parents[4],
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


def test_collector_preparation_never_controls_a_running_service_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[4], ava_home=home, roles=frozenset({"gateway"})
    )

    def no_root_control() -> None:
        pytest.fail("Preparation cannot independently reconcile a live root")

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)
    monkeypatch.setattr("cli.commands.lifecycle.root_driver.root_client", no_root_control)
    oc.ensure_otel_collector_step(ctx)


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
        repo=Path(__file__).resolve().parents[4],
        ava_home=home,
        roles=frozenset({"gateway"}),
    )

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)
    oc.ensure_otel_collector_step(ctx)

    assert config.read_text(encoding="utf-8") == "stale: true\n"
    assert "stale/residual" in capsys.readouterr().err


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
