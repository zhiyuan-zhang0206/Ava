from __future__ import annotations

from pathlib import Path

import pytest

from scripts.codegen import build_app_update_manifest as manifest_builder


def _artifact(directory: Path, name: str, signature: str | None = None) -> None:
    (directory / name).write_bytes(b"bundle")
    if signature is not None:
        (directory / f"{name}.sig").write_text(signature)


def test_builds_tauri_static_manifest_for_universal_macos(tmp_path: Path) -> None:
    _artifact(tmp_path, "Meiva.app.tar.gz", "mac-signature\n")
    _artifact(tmp_path, "Meiva_0.4.1_aarch64.dmg")
    _artifact(tmp_path, "Meiva_0.4.1.apk")

    manifest = manifest_builder.build_manifest(
        tag="app-v0.4.1",
        artifacts=tmp_path,
        repo="owner/Meiva",
    )

    assert manifest["version"] == "0.4.1"
    assert set(manifest["platforms"]) == {
        "darwin-aarch64",
        "darwin-x86_64",
    }
    assert manifest["platforms"]["darwin-aarch64"] == {
        "url": "https://github.com/owner/Meiva/releases/download/app-v0.4.1/Meiva.app.tar.gz",
        "signature": "mac-signature",
    }


def test_unsigned_build_still_emits_an_honest_empty_feed(tmp_path: Path) -> None:
    _artifact(tmp_path, "Meiva.app.tar.gz")

    manifest = manifest_builder.build_manifest(
        tag="app-v0.4.1",
        artifacts=tmp_path,
        repo="owner/Meiva",
    )

    assert manifest == {"version": "0.4.1", "platforms": {}}


@pytest.mark.parametrize("tag", ["v0.4.1", "app-v1", "app-v1.2.x", "app-v1.2.3.4"])
def test_rejects_tags_outside_the_app_semver_namespace(tmp_path: Path, tag: str) -> None:
    with pytest.raises(ValueError):
        manifest_builder.build_manifest(tag=tag, artifacts=tmp_path, repo="owner/Meiva")


def test_rejects_ambiguous_archives(tmp_path: Path) -> None:
    _artifact(tmp_path, "Meiva.app.tar.gz", "first")
    _artifact(tmp_path, "Meiva-copy.app.tar.gz", "second")

    with pytest.raises(ValueError, match="multiple macOS updater archives"):
        manifest_builder.build_manifest(
            tag="app-v0.4.1",
            artifacts=tmp_path,
            repo="owner/Meiva",
        )
