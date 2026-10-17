"""cli.commands.observability.otel_collector — binary install + config generation tests.

No network: the download is monkeypatched; the config render and the
idempotence marker are the logic under test. The data-plane receivers
(issue #46) are rendered against the REAL template so the placeholder and
YAML-indentation contract between template and generator is covered.
"""

from __future__ import annotations

import platform
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import yaml

import base.config as base_config
from base.deploy.release import collector_artifact as artifact
from cli.commands.observability import otel_collector as oc
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_platform_tag_maps_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned asset tags cover darwin/linux amd64+arm64; anything
    else is unsupported (None → sidecar skipped, agents auto-disable OTLP)."""
    cases = [
        ("Darwin", "arm64", "darwin_arm64"),
        ("Darwin", "x86_64", "darwin_amd64"),
        ("Linux", "x86_64", "linux_amd64"),
        ("Linux", "aarch64", "linux_arm64"),
        ("Linux", "i686", None),
    ]
    for system, machine, expected in cases:
        monkeypatch.setattr(platform, "system", lambda _s=system: _s)
        monkeypatch.setattr(platform, "machine", lambda _m=machine: _m)
        assert artifact.platform_tag() == expected


def test_generate_config_bakes_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Fan-out endpoints + retention are baked from settings; the template's
    placeholders are all consumed (no dangling $TOKEN)."""
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "meiva_home: $MEIVA_HOME\ntempo: $TEMPO_ENDPOINT\nloki: $LOKI_BASE\nprom: $PROM_BASE\nret: $RETENTION_DAYS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_tempo_endpoint", "http://10.0.0.2:14318"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_loki_url", "http://10.0.0.2:3100"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_prometheus_url", "http://10.0.0.2:9090"
    )
    monkeypatch.setattr(base_config.settings.observability, "trace_retention_days", 7)

    out = oc.generate_config(
        repo,
        Path("/home/u/.meiva"),
        roles=None,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    assert "meiva_home: /home/u/.meiva" in out
    assert "tempo: http://10.0.0.2:14318" in out
    assert "loki: http://10.0.0.2:3100/otlp" in out
    assert "prom: http://10.0.0.2:9090/api/v1/otlp" in out
    assert "ret: 7" in out
    assert "$" not in out


def test_generate_config_two_state_observability_url(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """MEIVA_OBSERVABILITY_URL set -> the gateway collector's LGTM fan-out points
    at the observatory station; unset -> the per-service settings URLs (loopback
    defaults, locked by test_generate_config_bakes_settings)."""
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "meiva_home: $MEIVA_HOME\ntempo: $TEMPO_ENDPOINT\nloki: $LOKI_BASE\nprom: $PROM_BASE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr(base_config.settings.observability, "observability_url", "http://10.0.0.46")
    # A remote observatory is a split-cluster shape: the relay authenticates
    # with the cluster bearer, so the secret must be set (empty fails closed).
    monkeypatch.setattr(base_config.settings.data_plane, "cluster_secret", "cluster-token")

    out = oc.generate_config(
        repo,
        Path("/home/u/.meiva"),
        roles=None,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    # WP4: a remote observatory is reached through the station's ONE
    # bearer-authenticated OTLP ingress, never the direct backend /otlp paths.
    assert "loki: http://10.0.0.46:4318" in out
    assert "prom: http://10.0.0.46:4318" in out
    assert "tempo: http://127.0.0.1:14318" in out

    monkeypatch.setattr(base_config.settings.observability, "observability_url", "")
    out = oc.generate_config(
        repo,
        Path("/home/u/.meiva"),
        roles=None,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    assert "loki: http://127.0.0.1:3100/otlp" in out
    assert "prom: http://127.0.0.1:9090/api/v1/otlp" in out


def test_write_config_preserves_user_edited_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A hand-edited config.yaml survives the next converge with a warning —
    the content-hash guard protects user edits (web-sources precedent)."""
    dest_dir = tmp_path / "otel-collector"
    dest_dir.mkdir(parents=True)
    config = dest_dir / "config.yaml"
    rendered = "otelcol: default\n"
    oc.write_config(config, rendered)
    assert config.read_text(encoding="utf-8") == rendered

    config.write_text("otelcol: user-edit\n", encoding="utf-8")
    oc.write_config(config, "otelcol: regenerated\n")

    assert config.read_text(encoding="utf-8") == "otelcol: user-edit\n"
    assert "modified locally" in capsys.readouterr().err


def test_write_config_records_and_accepts_converge_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_runtime: CommandRuntime,
) -> None:
    """After converge wrote a config, a re-render of the same content is a
    no-op; a re-render with NEW content replaces it (only user edits are
    protected)."""
    dest_dir = tmp_path / "otel-collector"
    dest_dir.mkdir(parents=True)
    config = dest_dir / "config.yaml"
    oc.write_config(config, "v1\n")
    oc.write_config(config, "v2\n")
    assert config.read_text(encoding="utf-8") == "v2\n"


def test_ensure_skips_download_when_version_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
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
        "ok: $MEIVA_HOME\n", encoding="utf-8"
    )

    downloaded: list[str] = []
    monkeypatch.setattr(
        artifact,
        "download_and_verify",
        lambda _tag, _dir: downloaded.append(_tag),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr(base_config.settings.observability, "trace_retention_days", 3)

    oc.ensure_otel_collector(
        repo,
        tmp_path,
        roles=None,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    assert downloaded == []
    assert (tmp_path / "otel-collector/config.yaml").exists()


def test_ensure_downloads_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """No binary -> download + verify run; the config is written after."""
    (tmp_path / "otel-collector").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "deploy/otel-collector").mkdir(parents=True)
    (repo / "deploy/otel-collector/otel-collector.yaml").write_text(
        "ok: $MEIVA_HOME\n", encoding="utf-8"
    )

    downloaded: list[str] = []
    monkeypatch.setattr(
        artifact,
        "download_and_verify",
        lambda tag, _d, **_ports: downloaded.append(tag),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_loki_url", "http://127.0.0.1:3100"
    )
    monkeypatch.setattr(
        base_config.settings.observability, "telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    monkeypatch.setattr(base_config.settings.observability, "trace_retention_days", 3)

    oc.ensure_otel_collector(
        repo,
        tmp_path,
        roles=None,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    assert len(downloaded) == 1
    assert (tmp_path / "otel-collector/config.yaml").exists()


def test_unsupported_platform_skips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """No pinned tag -> warn + skip, never download."""
    monkeypatch.setattr(artifact, "platform_tag", lambda: None)
    downloaded: list[str] = []
    monkeypatch.setattr(
        artifact,
        "download_and_verify",
        lambda _t, _d: downloaded.append(_t),  # pyright: ignore[reportUnknownArgumentType]
    )
    oc.ensure_otel_collector(
        tmp_path / "repo",
        tmp_path,
        roles=None,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    assert downloaded == []


_HOME = Path("/home/u/.meiva")
_PG_PORT = 5433
# The gateway login a start process adopts into MEIVA_DB_URL (port 1: never dialed).
_DELIVERED = "postgresql://meiva_g3_gateway:generation-login-password@127.0.0.1:1/meiva"


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
        base_config.settings.data_plane, "redis_url", "redis://meiva:runtime@127.0.0.1:6380/0"
    )
    monkeypatch.setattr(base_config.settings.data_plane, "redis_admin_password", "abc")


def render_real_template(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str] | None,
    *,
    gateway_url: str = "http://10.0.0.10:8000",
    machine_host: str = "10.0.0.10",
    cluster_secret: str = "cluster-token",  # noqa: S107 — fixture token
    self_metrics_port: int = 8888,
    otlp_enabled: bool = True,
    observability_url: str = "",
    operator_database: Callable[[], Any],
    command_runtime: CommandRuntime,
) -> dict[str, Any]:
    """Render the shipped template for `roles` and parse it as YAML."""
    _local_data_plane(monkeypatch)
    monkeypatch.setattr(base_config.settings.gateway, "gateway_url", gateway_url)
    from base.host.net.url_secret import url_with_host

    monkeypatch.setattr(
        base_config.settings.observability,
        "gateway_otlp_endpoint",
        url_with_host("http://localhost:4318", urlsplit(gateway_url).hostname or ""),
    )
    monkeypatch.setattr(base_config.settings.data_plane, "cluster_secret", cluster_secret)
    monkeypatch.setattr(
        base_config.settings.observability, "otel_collector_metrics_port", self_metrics_port
    )
    monkeypatch.setattr(base_config.settings.observability, "telemetry_otlp_enabled", otlp_enabled)
    monkeypatch.setattr(base_config.settings.observability, "observability_url", observability_url)
    command_runtime.machine.set(host=machine_host)
    command_runtime.machine.set(name="test-machine")
    repo = Path(__file__).resolve().parents[4]
    out = oc.generate_config(
        repo, _HOME, roles, database_factory=operator_database, machine=command_runtime.machine
    )
    # No placeholder left unconsumed. (A literal dollar survives on purpose:
    # the network-interface exclusion regexp's end-anchor, written $$ in the
    # template.)
    assert re.search(r"\$[A-Z_]{2,}", out) is None
    parsed: Any = yaml.safe_load(out)
    assert isinstance(parsed, dict)
    return parsed


def test_gateway_config_trace_mirror_rotation_policy(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The trace mirror's file exporter rotates by size with bounded backups:
    small segments (64 MiB — the structural bound on the ACTIVE file, since
    the file exporter exposes no time-based rotation) and a bounded number of
    backups; day retention comes from $RETENTION_DAYS (the cluster setting)."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
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
def test_sending_queues_are_in_memory_in_every_shape(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """No exporter queue is persisted: a restart drops what was queued (the trace
    mirror and `meiva trace ship` cover trace gaps), and only the filelog read
    offsets keep a file_storage extension."""
    cfg = render_real_template(
        monkeypatch, roles, operator_database=operator_database, command_runtime=command_runtime
    )

    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert "storage" not in cfg["exporters"][exporter_id]["sending_queue"]
    assert "file_storage" not in cfg["extensions"]
    assert "file_storage" not in cfg["service"]["extensions"]
    assert "file_storage/logoffsets" in cfg["extensions"]


def test_gateway_config_scrapes_this_clusters_own_data_plane(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A gateway-capable unit owns Postgres+Redis, so its sidecar carries the
    postgresql + redis receivers. Postgres is dialed DIRECT (never the pooler)
    over the home's owner-only socket as the password-less monitoring role,
    never as the write-generation login the start process adopted; Redis with its admin password."""
    from base.cluster.authority import MONITOR_ROLE
    from base.cluster.dataplane.pg_admin import pg_socket_path

    monkeypatch.setattr(base_config.settings.data_plane, "db_url", _DELIVERED)
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    rendered = yaml.safe_dump(cfg)
    assert "generation-login-password" not in rendered and "meiva_g3_gateway" not in rendered
    receivers = cfg["receivers"]
    socket_dir = pg_socket_path(_HOME).as_posix()
    # The receiver prefixes a unix endpoint's host with "/" itself.
    assert receivers["postgresql"]["endpoint"] == f"{socket_dir.lstrip('/')}:{_PG_PORT}"
    assert receivers["postgresql"]["transport"] == "unix"
    assert receivers["postgresql"]["username"] == MONITOR_ROLE
    assert receivers["postgresql"]["password"] == oc.PEER_PLACEHOLDER
    assert receivers["postgresql"]["databases"] == ["meiva"]
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
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Disabling OTLP export keeps Redis metrics but prevents PostgreSQL receiver startup."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        otlp_enabled=False,
        operator_database=operator_database,
        command_runtime=command_runtime,
    )

    assert "postgresql" not in cfg["receivers"]
    assert "redis" in cfg["receivers"]
    assert "postgresql" not in cfg["service"]["pipelines"]["metrics/infra"]["receivers"]


def test_remote_managed_plane_omits_the_postgres_receiver(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A remote-managed plane has no owner-only socket or monitoring role on
    this host (its provider monitors it), so only Redis is scraped; the
    provider's database credential never reaches the config."""
    monkeypatch.setattr(
        base_config.settings.data_plane,
        "db_url",
        "postgresql://meiva:provider-password@10.0.0.2:5433/meiva",
    )
    monkeypatch.setattr(base_config.settings.data_plane, "redis_url", "redis://10.0.0.2:6380/0")
    monkeypatch.setattr(base_config.settings.data_plane, "redis_admin_password", "abc")
    monkeypatch.setattr(base_config.settings.gateway, "gateway_url", "http://localhost:8000")
    monkeypatch.setattr(base_config.settings.data_plane, "cluster_secret", "")
    command_runtime.machine.set(host="localhost")
    command_runtime.machine.set(name="test-machine")
    repo = Path(__file__).resolve().parents[4]

    rendered = oc.generate_config(
        repo,
        _HOME,
        frozenset({"gateway", "agent-runner"}),
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
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
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A pure agent-runner's DB/Redis URLs point at the GATEWAY's data plane.
    Scraping from there would duplicate the gateway's own series under a
    second `host` / `machine_name` identity, so the two receivers are omitted
    entirely — host metrics, which ARE this machine's, stay."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"agent-runner"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    assert "postgresql" not in cfg["receivers"]
    assert "redis" not in cfg["receivers"]
    assert "host_metrics" in cfg["receivers"]
    assert cfg["service"]["pipelines"]["metrics/infra"]["receivers"] == [
        "host_metrics",
        "prometheus/otelcol",
    ]


def test_unconfigured_unit_has_no_data_plane_receivers(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """roles=None is a unit converge has not configured yet — there are no
    cluster URLs to read, so nothing data-plane is rendered."""
    cfg = render_real_template(
        monkeypatch, None, operator_database=operator_database, command_runtime=command_runtime
    )
    assert "postgresql" not in cfg["receivers"]
    assert "redis" not in cfg["receivers"]


def test_infra_metrics_ride_their_own_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """App metrics arrive over OTLP already carrying machine/agent_id
    attributes and must not be relabelled; the host-identity processors
    therefore sit on the infra pipeline only."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    pipelines = cfg["service"]["pipelines"]
    assert pipelines["metrics"]["receivers"] == ["otlp", "otlp/remote"]
    assert "transform/host_label" not in pipelines["metrics"]["processors"]
    assert "transform/host_label" in pipelines["metrics/infra"]["processors"]
    assert "resource_detection/host" in pipelines["metrics/infra"]["processors"]


def test_infra_pipeline_stamps_machine_name(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Infra datapoints keep physical host identity and gain Meiva roster identity."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    statements = cfg["processors"]["transform/host_label"]["metric_statements"][0]["statements"]
    assert 'set(attributes["host"], resource.attributes["host.name"])' in statements
    assert 'set(attributes["machine_name"], "test-machine")' in statements


def test_collector_self_metrics_reader_is_per_unit(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Each unit binds and scrapes its own configurable collector metrics port."""
    default_cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    default_reader = default_cfg["service"]["telemetry"]["metrics"]["readers"][0]["pull"][
        "exporter"
    ]["prometheus"]
    assert default_reader == {"host": "localhost", "port": 8888}

    override_cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        self_metrics_port=8889,
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    override_reader = override_cfg["service"]["telemetry"]["metrics"]["readers"][0]["pull"][
        "exporter"
    ]["prometheus"]
    assert override_reader == {"host": "localhost", "port": 8889}
    override_scrape = override_cfg["receivers"]["prometheus/otelcol"]["config"]["scrape_configs"]
    assert override_scrape[0]["static_configs"] == [{"targets": ["localhost:8889"]}]


def test_collector_internal_logs_are_warn_level(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """Exporter retry notices stay out of service stdout while warnings remain."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )
    assert cfg["service"]["telemetry"]["logs"]["level"] == "warn"


def test_logs_merge_event_and_filelog_transforms_before_batch(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The logs pipeline promotes bounded event dimensions and labels tailed
    session files before the final batch processor."""
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )

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
        "directory": "/home/u/.meiva/otel-collector/log-offsets",
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
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    cfg = render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        operator_database=operator_database,
        command_runtime=command_runtime,
    )

    processor = cfg["processors"]["filter/cluster_allow"]
    expected = [
        'resource.attributes["cluster"] != nil and resource.attributes["cluster"] != ".meiva"'
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
