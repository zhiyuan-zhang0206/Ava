"""cli.commands._otel_collector — binary install + config generation tests.

No network: the download is monkeypatched; the config render and the
idempotence marker are the logic under test. The data-plane receivers
(issue #46) are rendered against the REAL template so the placeholder and
YAML-indentation contract between template and generator is covered.
"""

from __future__ import annotations

import platform
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from cli.commands import _otel_collector as oc


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
    obs = pytest.MonkeyPatch()
    obs.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://10.0.0.2:14318"
    )
    obs.setattr("shared.config.settings.observability.telemetry_loki_url", "http://10.0.0.2:3100")
    obs.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://10.0.0.2:9090"
    )
    obs.setattr("shared.config.settings.observability.trace_retention_days", 7)

    out = oc.generate_config(repo, Path("/home/u/.ava"), roles=None)
    assert "ava_home: /home/u/.ava" in out
    assert "tempo: http://10.0.0.2:14318" in out
    assert "loki: http://10.0.0.2:3100/otlp" in out
    assert "prom: http://10.0.0.2:9090/api/v1/otlp" in out
    assert "ret: 7" in out
    assert "$" not in out


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
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        oc,
        "_download_and_verify",
        lambda _tag, _dir: downloaded.append(_tag),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    )

    obs = pytest.MonkeyPatch()
    obs.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    obs.setattr("shared.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100")
    obs.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    obs.setattr("shared.config.settings.observability.trace_retention_days", 3)

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
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        oc,
        "_download_and_verify",
        lambda tag, _d: downloaded.append(tag),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    )

    obs = pytest.MonkeyPatch()
    obs.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://127.0.0.1:14318"
    )
    obs.setattr("shared.config.settings.observability.telemetry_loki_url", "http://127.0.0.1:3100")
    obs.setattr(
        "shared.config.settings.observability.telemetry_prometheus_url", "http://127.0.0.1:9090"
    )
    obs.setattr("shared.config.settings.observability.trace_retention_days", 3)

    oc.ensure_otel_collector(repo, tmp_path, roles=None)
    assert len(downloaded) == 1
    assert (tmp_path / "otel-collector/config.yaml").exists()


def test_unsupported_platform_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No pinned tag -> warn + skip, never download."""
    monkeypatch.setattr(oc, "platform_tag", lambda: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    downloaded: list[str] = []
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        oc,
        "_download_and_verify",
        lambda _t, _d: downloaded.append(_t),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    )
    oc.ensure_otel_collector(tmp_path / "repo", tmp_path, roles=None)
    assert downloaded == []


def _render_real_template(
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str] | None,
    *,
    gateway_url: str = "http://100.64.0.10:8000",
    machine_host: str = "100.64.0.10",
    cluster_secret: str = "cluster-token",  # noqa: S107 — fixture token
) -> dict[str, Any]:
    """Render the shipped template for `roles` and parse it as YAML."""
    monkeypatch.setattr(
        "shared.db.direct_db_url", lambda: "postgresql://ava:s3cr3t@10.0.0.2:5433/ava"
    )
    monkeypatch.setattr(
        "shared.config.settings.data_plane.redis_url", "redis://:s3cr3t@10.0.0.2:6380/0"
    )
    monkeypatch.setattr("shared.config.settings.gateway.gateway_url", gateway_url)
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", cluster_secret)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: machine_host)
    repo = Path(__file__).resolve().parents[2]
    out = oc.generate_config(repo, Path("/home/u/.ava"), roles)
    # No placeholder left unconsumed. (A literal dollar survives on purpose:
    # the network-interface exclusion regexp's end-anchor, written $$ in the
    # template.)
    assert re.search(r"\$[A-Z_]{2,}", out) is None
    parsed: Any = yaml.safe_load(out)
    assert isinstance(parsed, dict)
    return parsed


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
    assert receivers["postgresql"]["password"] == "s3cr3t"  # noqa: S105 — fixture value
    assert receivers["postgresql"]["databases"] == ["ava"]
    assert receivers["redis"]["endpoint"] == "10.0.0.2:6380"
    assert receivers["redis"]["password"] == "s3cr3t"  # noqa: S105 — fixture value
    infra = cfg["service"]["pipelines"]["metrics/infra"]
    assert infra["receivers"] == [
        "host_metrics",
        "prometheus/otelcol",
        "postgresql",
        "redis",
    ]


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
    second `host` label, so the two receivers are omitted entirely — host
    metrics, which ARE this machine's, stay."""
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


def test_session_filelog_excludes_collectors_own_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector tails every service session except its own output, which
    would otherwise be re-ingested recursively through the logs exporter."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))

    receiver = cfg["receivers"]["filelog/sessions"]
    assert receiver["include"] == ["/home/u/.ava/logs/*.out.log"]
    assert receiver["exclude"] == [
        "/home/u/.ava/logs/ava-otel-collector.out.log",
    ]


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
        assert exporter["endpoint"] == "http://100.64.0.10:4318"
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
        "endpoint": "100.64.0.10:4318",
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


def test_hybrid_gateway_runner_still_serves_remote_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway capability plus a non-empty secret is the cross-machine posture
    even when the same host also runs agents (the production Mac mini shape)."""
    cfg = _render_real_template(monkeypatch, frozenset({"gateway", "agent-runner"}))
    remote = cfg["receivers"]["otlp/remote"]["protocols"]["http"]
    assert remote["endpoint"] == "100.64.0.10:4318"
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


@pytest.mark.parametrize(
    ("roles", "gateway_url", "machine_host", "cluster_secret", "message"),
    [
        (
            frozenset({"agent-runner"}),
            "http://localhost:8000",
            "100.64.0.20",
            "token",
            "gateway URL",
        ),
        (frozenset({"agent-runner"}), "http://0.0.0.0:8000", "100.64.0.20", "token", "gateway URL"),
        (frozenset({"agent-runner"}), "http://[::]:8000", "100.64.0.20", "token", "gateway URL"),
        (
            frozenset({"agent-runner"}),
            "http://100.64.0.10:8000",
            "100.64.0.20",
            "",
            "cluster secret",
        ),
        (frozenset({"gateway"}), "http://100.64.0.10:8000", "localhost", "token", "reachable host"),
        (frozenset({"gateway"}), "http://100.64.0.10:8000", "100.64.0.10", "", "cluster secret"),
        (frozenset({"gateway"}), "http://100.64.0.10:8000", "0.0.0.0", "token", "reachable host"),  # noqa: S104 — rejection fixture
        (
            frozenset({"gateway"}),
            "http://[fd7a:115c:a1e0::10]:8000",
            "::",
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
            "static_configs": [{"targets": ["127.0.0.1:8888"]}],
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
    monkeypatch.setattr(oc, "_download_and_verify", lambda _tag, _dir: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        "shared.db.direct_db_url", lambda: "postgresql://ava:s3cr3t@10.0.0.2:5433/ava"
    )
    monkeypatch.setattr(
        "shared.config.settings.data_plane.redis_url", "redis://:s3cr3t@10.0.0.2:6380/0"
    )
    monkeypatch.setattr("shared.config.settings.gateway.gateway_url", "http://100.64.0.10:8000")
    monkeypatch.setattr("shared.config.settings.data_plane.cluster_secret", "cluster-token")
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "100.64.0.10")
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
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]  # no real backoff wait

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
    monkeypatch.setattr(oc.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    oc._download_with_retry("https://example.invalid/t.tar.gz", tmp_path / "t.tar.gz")
    assert calls["n"] == 2
    assert (tmp_path / "t.tar.gz").read_bytes() == b"ok"
