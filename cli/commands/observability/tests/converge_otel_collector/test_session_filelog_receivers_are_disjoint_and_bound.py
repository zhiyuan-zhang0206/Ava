"""Converge otel collector cases: session filelog receivers are disjoint and bound."""

from __future__ import annotations

import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.deploy.release import collector_artifact as artifact
from cli.commands.observability import otel_collector as oc
from cli.commands.observability.tests.test_converge_otel_collector import (
    _ChunkedResp,
    _render_real_template,
)
from tests.path_scoped.cli_tests import operator_database as operator_database


def test_session_filelog_receivers_are_disjoint_and_bound_discovery(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """Shell transcripts and service output use disjoint file sets.

    Agent main logs begin with identical telemetry banners, so admitting them
    to either receiver would restore the fingerprint-collision re-watch storm.
    """
    cfg = _render_real_template(
        monkeypatch, frozenset({"gateway", "agent-runner"}), operator_database=operator_database
    )

    sessions = cfg["receivers"]["filelog/sessions"]
    assert sessions == {
        "include": ["/home/u/.ava/logs/ava-agent-*-shell-*.out.log"],
        "start_at": "end",
        "include_file_name": True,
        "include_file_path": False,
        "storage": "file_storage/logoffsets",
        "poll_interval": "30s",
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
        "poll_interval": "30s",
        "polls_to_archive": 50,
        "max_concurrent_files": 200,
    }


def test_runner_forwards_to_authenticated_gateway_ingress_without_renaming_queues(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """A pure runner relays all signals to the gateway collector, never to
    loopback backends. Exporter IDs stay byte-for-byte stable so a converge
    adopts the existing file_storage queues instead of orphaning their backlog."""
    cfg = _render_real_template(
        monkeypatch, frozenset({"agent-runner"}), operator_database=operator_database
    )

    assert set(cfg["receivers"]) >= {"otlp", "host_metrics", "prometheus/otelcol"}
    assert "otlp/remote" not in cfg["receivers"]
    exporters = cfg["exporters"]
    expected_ids = {"otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus", "file/traces"}
    assert set(exporters) == expected_ids
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        exporter = exporters[exporter_id]
        assert exporter["endpoint"] == "http://10.0.0.10:4318"
        assert exporter["headers"] == {"Authorization": f"Bearer {oc.telemetry_bearer()}"}
    assert exporters["otlphttp/tempo"]["sending_queue"]["storage"] == "file_storage"
    assert exporters["otlphttp/loki"]["sending_queue"]["storage"] == "file_storage"
    assert "storage" not in exporters["otlphttp/prometheus"]["sending_queue"]
    rendered = str(cfg)
    assert "127.0.0.1:14318" not in rendered
    assert "127.0.0.1:3100" not in rendered
    assert "127.0.0.1:9090" not in rendered


def test_gateway_has_separate_authenticated_reachable_receiver(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """A split gateway keeps local producers on loopback/no-auth and accepts
    remote relays only on its declared reachable address with bearer auth."""
    cfg = _render_real_template(
        monkeypatch, frozenset({"gateway"}), operator_database=operator_database
    )

    assert cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4318"}
    assert cfg["receivers"]["otlp/remote"]["protocols"]["http"] == {
        "endpoint": "10.0.0.10:4318",
        "auth": {"authenticator": "bearertokenauth/cluster"},
    }
    assert cfg["extensions"]["bearertokenauth/cluster"] == {"token": oc.telemetry_bearer()}
    assert oc.telemetry_bearer() not in ("", "cluster-token")  # derived, never the secret
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
    operator_database: Callable[[], Any],
) -> None:
    """A pure observability-station exposes the same bearer-authenticated
    remote OTLP ingress a gateway does — the surface remote gateway
    collectors relay to (WP4, task #1946)."""
    cfg = _render_real_template(
        monkeypatch, frozenset({"observability-station"}), operator_database=operator_database
    )

    assert cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4318"}
    assert cfg["receivers"]["otlp/remote"]["protocols"]["http"] == {
        "endpoint": "10.0.0.10:4318",
        "auth": {"authenticator": "bearertokenauth/cluster"},
    }
    assert cfg["extensions"]["bearertokenauth/cluster"] == {"token": oc.telemetry_bearer()}
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
    operator_database: Callable[[], Any],
) -> None:
    """A gateway consuming a remote observatory (AVA_OBSERVABILITY_URL set)
    relays every signal to the station's single bearer-authenticated OTLP
    ingress with the cluster bearer — it never dials the station's
    loopback-bound backends directly (WP4, task #1946)."""
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        observability_url="http://10.0.0.46",
        operator_database=operator_database,
    )
    exporters = cfg["exporters"]
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        exporter = exporters[exporter_id]
        assert exporter["endpoint"] == "http://10.0.0.46:4318"
        assert exporter["headers"] == {"Authorization": f"Bearer {oc.telemetry_bearer()}"}
    rendered = str(cfg)
    assert "127.0.0.1:3100" not in rendered
    assert "127.0.0.1:9090" not in rendered
    assert "127.0.0.1:14318" not in rendered
    # The gateway keeps its local trace mirror and its local loopback receiver.
    assert "file/traces" in exporters
    assert cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4318"}


def test_remote_observatory_relay_without_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """A remote observatory with no cluster secret cannot authenticate the
    relay — converge must fail, not ship an unauthenticated fan-out."""
    with pytest.raises(RuntimeError, match="telemetry token"):
        _render_real_template(
            monkeypatch,
            frozenset({"gateway", "agent-runner"}),
            cluster_secret="",
            observability_url="http://10.0.0.46",
            operator_database=operator_database,
        )


def test_station_otlp_ingress_port_follows_single_source(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """The station's advertised unit url (base.cluster.machines.unit_dial_url) and
    its remote receiver bind the SAME port — AVA_TELEMETRY_OTLP_PORT is the
    single knob for both (WP4, task #1946)."""
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_port", 4321)
    cfg = _render_real_template(
        monkeypatch, frozenset({"observability-station"}), operator_database=operator_database
    )
    assert cfg["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"] == ("10.0.0.10:4321")
    from base.cluster.machines import unit_dial_url

    assert unit_dial_url(frozenset({"observability-station"})) == "http://10.0.0.10:4321"


def test_local_ingress_port_does_not_change_gateway_projection(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """Local receivers follow this unit's port; relay targets follow bootstrap."""
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_port", 4319)
    gateway_cfg = _render_real_template(
        monkeypatch, frozenset({"gateway"}), operator_database=operator_database
    )
    assert gateway_cfg["receivers"]["otlp"]["protocols"]["http"] == {"endpoint": "127.0.0.1:4319"}
    assert gateway_cfg["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"] == (
        "10.0.0.10:4319"
    )
    runner_cfg = _render_real_template(
        monkeypatch, frozenset({"agent-runner"}), operator_database=operator_database
    )
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert runner_cfg["exporters"][exporter_id]["endpoint"] == "http://10.0.0.10:4318"


def test_hybrid_gateway_runner_still_serves_remote_runners(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """Gateway capability plus a non-empty secret is the cross-machine posture
    even when the same host also runs agents (the production Mac mini shape)."""
    cfg = _render_real_template(
        monkeypatch, frozenset({"gateway", "agent-runner"}), operator_database=operator_database
    )
    remote = cfg["receivers"]["otlp/remote"]["protocols"]["http"]
    assert remote["endpoint"] == "10.0.0.10:4318"
    assert remote["auth"] == {"authenticator": "bearertokenauth/cluster"}


def test_gateway_ipv6_receiver_uses_unambiguous_bracketed_bind(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway"}),
        gateway_url="http://[fd7a:115c:a1e0::10]:8000",
        machine_host="fd7a:115c:a1e0::10",
        operator_database=operator_database,
    )
    assert (
        cfg["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"]
        == "[fd7a:115c:a1e0::10]:4318"
    )


def test_runner_ipv6_gateway_url_uses_unambiguous_bracketed_exporter_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"agent-runner"}),
        gateway_url="http://[fd7a:115c:a1e0::10]:8000",
        machine_host="fd7a:115c:a1e0::20",
        operator_database=operator_database,
    )
    for exporter_id in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert cfg["exporters"][exporter_id]["endpoint"] == "http://[fd7a:115c:a1e0::10]:4318"


def test_single_box_keeps_every_otlp_listener_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """The zero-config combined role has no cross-machine ingress or secret."""
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"gateway", "agent-runner"}),
        gateway_url="http://localhost:8000",
        machine_host="localhost",
        cluster_secret="",
        operator_database=operator_database,
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
    operator_database: Callable[[], Any],
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
        operator_database=operator_database,
    )
    assert "otlp/remote" not in cfg["receivers"]
    assert "bearertokenauth/cluster" not in cfg["extensions"]
    assert cfg["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4318"


def test_pure_role_units_collapse_remote_ingress_without_remote_identity(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
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
            operator_database=operator_database,
        )
        assert "otlp/remote" not in no_secret["receivers"]
        assert "bearertokenauth/cluster" not in no_secret["extensions"]

        loopback = _render_real_template(
            monkeypatch,
            roles,
            cluster_secret="cluster-token",  # noqa: S106 — fixture token
            machine_host="localhost",
            operator_database=operator_database,
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
            "gateway bootstrap",
        ),
        (
            frozenset({"agent-runner"}),
            "http://0.0.0.0:8000",
            "10.0.0.20",
            "token",
            "gateway bootstrap",
        ),
        (
            frozenset({"agent-runner"}),
            "http://[::]:8000",
            "10.0.0.20",
            "token",
            "gateway bootstrap",
        ),
        (
            frozenset({"agent-runner"}),
            "http://10.0.0.10:8000",
            "10.0.0.20",
            "",
            "telemetry token",
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
    operator_database: Callable[[], Any],
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _render_real_template(
            monkeypatch,
            roles,
            gateway_url=gateway_url,
            machine_host=machine_host,
            cluster_secret=cluster_secret,
            operator_database=operator_database,
        )


def test_collector_self_metrics_are_scraped_for_queue_and_drop_visibility(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
) -> None:
    """The collector's own queue depth/capacity/enqueue-failure counters ride
    the infra pipeline, so a recovered path carries evidence of the outage."""
    cfg = _render_real_template(
        monkeypatch, frozenset({"agent-runner"}), operator_database=operator_database
    )
    scrape = cfg["receivers"]["prometheus/otelcol"]["config"]["scrape_configs"]
    assert scrape == [
        {
            "job_name": "ava-otel-collector",
            "scrape_interval": "30s",
            "static_configs": [{"targets": ["localhost:8888"]}],
        }
    ]


def test_config_file_is_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator_database: Callable[[], Any]
) -> None:
    """Every split role's config carries the telemetry bearer (never the secret) and a gateway's
    the Redis admin password; the file is 0600 from creation, not chmod-ed after."""
    if platform.system() == "Windows":
        pytest.skip("POSIX file modes only")
    (tmp_path / "otel-collector").mkdir(parents=True)
    (tmp_path / "otel-collector/otelcol-contrib").write_bytes(b"bin")
    (tmp_path / "otel-collector/version").write_text(
        artifact.OTELCOL_CONTRIB_VERSION, encoding="utf-8"
    )
    monkeypatch.setattr(artifact, "download_and_verify", lambda _tag, _dir: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "base.config.settings.observability.gateway_otlp_endpoint", "http://10.0.0.10:4318"
    )
    monkeypatch.setattr("base.config.settings.data_plane.cluster_secret", "cluster-token")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.10")
    repo = Path(__file__).resolve().parents[5]

    replace = Path.replace
    modes_before_publish: list[int] = []

    def _record_replace(source: Path, target: Path) -> Path:
        modes_before_publish.append(source.stat().st_mode & 0o777)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", _record_replace)
    oc.ensure_otel_collector(
        repo, tmp_path, frozenset({"agent-runner"}), database_factory=operator_database
    )
    config = tmp_path / "otel-collector/config.yaml"
    assert modes_before_publish == [0o600]
    assert config.stat().st_mode & 0o777 == 0o600
    assert f"Bearer {oc.telemetry_bearer()}" in config.read_text(encoding="utf-8")


def test_stream_download_writes_all_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The streamed bytes land in the tarball path; the final line reports the
    size; no heartbeat fires for a fast download."""
    payload = b"x" * (1 << 20)  # 1 MiB

    def _fake_urlopen(_url: str, **kw: object) -> _ChunkedResp:
        return _ChunkedResp([payload] * 4, {"Content-Length": str(4 << 20)})

    monkeypatch.setattr(artifact.urllib.request, "urlopen", _fake_urlopen)
    dest = tmp_path / "t.tar.gz"

    artifact._stream_download("https://example.invalid/t.tar.gz", dest)

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

    monkeypatch.setattr(artifact.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(TimeoutError):
        artifact._stream_download("https://example.invalid/t.tar.gz", tmp_path / "t.tar.gz")
    assert seen["timeout"] == artifact._DOWNLOAD_SOCKET_TIMEOUT_S


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
    operator_database: Callable[[], Any],
) -> None:
    """Every collector-to-downstream hop fails fast when its queue is full.

    Persistent trace/log queues retain their stable IDs and bounded capacity,
    but never block an OTLP receiver waiting for queue space or downstream
    completion. Each send attempt and the whole retry sequence are bounded;
    retry exhaustion drops the batch through the collector's counted failure
    path. Metrics keep their existing 15-minute retry policy in memory.
    """
    cfg = _render_real_template(monkeypatch, roles, operator_database=operator_database)
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
