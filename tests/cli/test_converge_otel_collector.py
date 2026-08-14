"""cli.commands._otel_collector — binary install + config generation tests.

No network: the download is monkeypatched; the config render and the
idempotence marker are the logic under test.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

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

    out = oc.generate_config(repo, Path("/home/u/.ava"))
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

    oc.ensure_otel_collector(repo, tmp_path)
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

    oc.ensure_otel_collector(repo, tmp_path)
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
    oc.ensure_otel_collector(tmp_path / "repo", tmp_path)
    assert downloaded == []
