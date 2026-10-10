"""Pinned native LGTM release assets and supported platform selection."""

from pathlib import Path

import pytest
import yaml

from cli.commands.observability import lgtm_native


def _repo() -> Path:
    return Path(__file__).resolve().parents[4]


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
            "version": "13.2.3",
            "assets": {
                "darwin-arm64": {
                    "url": "https://dl.grafana.com/oss/release/grafana-13.2.3.darwin-arm64.tar.gz",
                    "sha256": "248a51bcfacdb1ec642006cee46b4de44a34bcf41ddea88d96a4605e2e9d808c",
                }
            },
        },
    }


def test_platform_tag_supports_darwin_arm64_and_linux_amd64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lgtm_native.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lgtm_native.platform, "machine", lambda: "arm64")
    assert lgtm_native.platform_tag() == "darwin_arm64"

    monkeypatch.setattr(lgtm_native.platform, "system", lambda: "Linux")
    assert lgtm_native.platform_tag() is None
    monkeypatch.setattr(lgtm_native.platform, "machine", lambda: "x86_64")
    assert lgtm_native.platform_tag() == "linux_amd64"
