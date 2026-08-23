"""Native LGTM backend installer and launchd rendering tests."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
import yaml

from cli.commands import _lgtm_native
from cli.commands._converge_spec import ConvergeCtx
from shared.loki_index_labels import validate_loki_deploy_config


def _repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _native_dir(home: Path) -> Path:
    return home / "lgtm" / "native"


def _mark_current(home: Path) -> None:
    native_dir = _native_dir(home)
    for name, spec in _lgtm_native._load_versions(_repo()).items():
        (native_dir / f"version-{name}").parent.mkdir(parents=True, exist_ok=True)
        (native_dir / f"version-{name}").write_text(spec["version"] + "\n", encoding="utf-8")


def _redirect_plists(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def fake_plist_path(name: str) -> Path:
        return root / f"com.ava.{name}.plist"

    monkeypatch.setattr(
        _lgtm_native,
        "_plist_path",
        fake_plist_path,
    )


def test_versions_file_has_the_pinned_release_assets() -> None:
    versions_path = _repo() / "deploy/lgtm/native/versions.yml"
    versions = yaml.safe_load(versions_path.read_text(encoding="utf-8"))

    assert versions == {
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


def test_platform_tag_supports_only_darwin_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_lgtm_native.platform, "machine", lambda: "arm64")
    assert _lgtm_native.platform_tag() == "darwin_arm64"

    monkeypatch.setattr(_lgtm_native.platform, "system", lambda: "Linux")
    assert _lgtm_native.platform_tag() is None


def test_ensure_skips_download_when_markers_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _mark_current(home)
    _redirect_plists(monkeypatch, tmp_path / "plists")
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")

    def fail_download(_name: str, _version: str, _asset: dict[str, str], _native_dir: Path) -> None:
        pytest.fail("current marker must skip the download")

    monkeypatch.setattr(
        _lgtm_native,
        "_download_and_verify",
        fail_download,
    )

    _lgtm_native.ensure_lgtm_native(_repo(), home)

    assert (home / "lgtm/native/config/loki.yaml").exists()


def test_ensure_downloads_when_a_marker_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _mark_current(home)
    (_native_dir(home) / "version-loki").write_text("old\n", encoding="utf-8")
    _redirect_plists(monkeypatch, tmp_path / "plists")
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

    _lgtm_native.ensure_lgtm_native(_repo(), home)

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


def test_ensure_renders_configs_with_native_paths_and_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _mark_current(home)
    _redirect_plists(monkeypatch, tmp_path / "plists")
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")

    _lgtm_native.ensure_lgtm_native(_repo(), home)

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
    }
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
    assert "retention_period: 168h" in loki
    validate_loki_deploy_config(loki_config)
    assert "query_timeout: 50s" in loki
    assert "max_entries_limit_per_query: 50001" in loki
    assert "split_queries_by_interval: 24h" in loki
    targets = {
        job["job_name"]: job["static_configs"][0]["targets"]
        for job in prometheus_config["scrape_configs"]
    }
    assert targets == {
        "prometheus": ["localhost:9090"],
        "tempo": ["127.0.0.1:3200"],
        "loki": ["127.0.0.1:3100"],
        "grafana": ["127.0.0.1:3003"],
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


def test_render_plist_uses_absolute_paths_and_memory_caps(tmp_path: Path) -> None:
    native_dir = tmp_path / "home/lgtm/native"
    home = tmp_path / "home"

    expected_limits = {"loki": "2GiB", "prometheus": "1GiB"}
    for name, expected_limit in expected_limits.items():
        rendered = _lgtm_native._render_plist(name, native_dir, home)
        plist = plistlib.loads(rendered.encode())
        assert plist["Label"] == _lgtm_native.native_label(name, home)
        assert plist["EnvironmentVariables"]["GOMEMLIMIT"] == expected_limit
        assert Path(plist["ProgramArguments"][0]).is_absolute()
        assert plist["StandardOutPath"] == str(home / f"lgtm/native/logs/{name}.log")
        assert plist["StandardErrorPath"] == str(home / f"lgtm/native/logs/{name}.log")


def test_native_step_does_not_touch_an_unmarked_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = ConvergeCtx(repo=_repo(), ava_home=tmp_path / "home", roles=frozenset({"gateway"}))

    def fail_ensure(_repo_path: Path, _home: Path) -> None:
        pytest.fail("unmarked homes must be a no-op")

    monkeypatch.setattr(
        _lgtm_native,
        "ensure_lgtm_native",
        fail_ensure,
    )

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert not ctx.ava_home.exists()
