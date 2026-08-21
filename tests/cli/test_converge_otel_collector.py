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
    monkeypatch: pytest.MonkeyPatch, roles: frozenset[str] | None
) -> dict[str, Any]:
    """Render the shipped template for `roles` and parse it as YAML."""
    monkeypatch.setattr(
        "shared.db.direct_db_url", lambda: "postgresql://ava:s3cr3t@10.0.0.2:5433/ava"
    )
    monkeypatch.setattr(
        "shared.config.settings.data_plane.redis_url", "redis://:s3cr3t@10.0.0.2:6380/0"
    )
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
    assert infra["receivers"] == ["host_metrics", "postgresql", "redis"]


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
    assert cfg["service"]["pipelines"]["metrics/infra"]["receivers"] == ["host_metrics"]


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
    assert pipelines["metrics"]["receivers"] == ["otlp"]
    assert "transform/host_label" not in pipelines["metrics"]["processors"]
    assert "transform/host_label" in pipelines["metrics/infra"]["processors"]
    assert "resource_detection/host" in pipelines["metrics/infra"]["processors"]


def test_config_file_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On a gateway the rendered config carries the cluster secret (pg/redis
    receiver credentials), so it gets the same 0600 the cluster .env has."""
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
    repo = Path(__file__).resolve().parents[2]

    oc.ensure_otel_collector(repo, tmp_path, frozenset({"gateway"}))
    config = tmp_path / "otel-collector/config.yaml"
    assert config.stat().st_mode & 0o777 == 0o600
    assert "s3cr3t" in config.read_text(encoding="utf-8")


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
