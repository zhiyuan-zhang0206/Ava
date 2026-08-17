from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "build_shell_update_manifest.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_shell_update_manifest", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest_builder = _load_script()


def _artifact(directory: Path, name: str, signature: str | None = None) -> None:
    (directory / name).write_bytes(b"bundle")
    if signature is not None:
        (directory / f"{name}.sig").write_text(signature)


def test_builds_tauri_static_manifest_for_universal_macos_and_nsis(tmp_path: Path) -> None:
    _artifact(tmp_path, "Ava.app.tar.gz", "mac-signature\n")
    _artifact(tmp_path, "Ava_0.4.1_x64-setup.nsis.zip", "windows-signature\n")
    # Installers and the Android download are release assets, not updater archives.
    _artifact(tmp_path, "Ava_0.4.1_aarch64.dmg")
    _artifact(tmp_path, "Ava_0.4.1.apk")

    manifest = manifest_builder.build_manifest(
        tag="shell-v0.4.1",
        artifacts=tmp_path,
        repo="owner/Ava",
    )

    assert manifest["version"] == "0.4.1"
    assert set(manifest["platforms"]) == {
        "darwin-aarch64",
        "darwin-x86_64",
        "windows-x86_64",
        "windows-x86_64-nsis",
    }
    assert manifest["platforms"]["darwin-aarch64"] == {
        "url": "https://github.com/owner/Ava/releases/download/shell-v0.4.1/Ava.app.tar.gz",
        "signature": "mac-signature",
    }
    assert manifest["platforms"]["windows-x86_64-nsis"]["signature"] == ("windows-signature")


def test_unsigned_build_still_emits_an_honest_empty_feed(tmp_path: Path) -> None:
    _artifact(tmp_path, "Ava.app.tar.gz")
    _artifact(tmp_path, "Ava_0.4.1_x64-setup.nsis.zip")

    manifest = manifest_builder.build_manifest(
        tag="shell-v0.4.1",
        artifacts=tmp_path,
        repo="owner/Ava",
    )

    assert manifest == {"version": "0.4.1", "platforms": {}}


@pytest.mark.parametrize("tag", ["v0.4.1", "shell-v1", "shell-v1.2.x", "shell-v1.2.3.4"])
def test_rejects_tags_outside_the_shell_semver_namespace(tmp_path: Path, tag: str) -> None:
    with pytest.raises(ValueError):
        manifest_builder.build_manifest(tag=tag, artifacts=tmp_path, repo="owner/Ava")


def test_rejects_ambiguous_archives(tmp_path: Path) -> None:
    _artifact(tmp_path, "Ava.app.tar.gz", "first")
    _artifact(tmp_path, "Ava-copy.app.tar.gz", "second")

    with pytest.raises(ValueError, match="multiple macOS updater archives"):
        manifest_builder.build_manifest(
            tag="shell-v0.4.1",
            artifacts=tmp_path,
            repo="owner/Ava",
        )
