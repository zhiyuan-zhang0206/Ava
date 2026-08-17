"""The Android overlay patcher (`ui/app/android/apply_overlay.py`).

The overlay edits a file this repo does not own — the AndroidManifest.xml that
`cargo tauri android init` generates — and only the release workflow ever runs
it, on a machine with an Android SDK. Nothing else would catch a patcher that
silently stopped applying, so the manifest transform is tested here against the
real Tauri 2 template shape.

The fixture below is the template verbatim (tauri-cli 2.11
`templates/mobile/android/app/src/main/AndroidManifest.xml`) with its handlebars
placeholders filled in the way the generator fills them.
"""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OVERLAY = _REPO_ROOT / "ui" / "app" / "android" / "apply_overlay.py"
_ANDROID_NS = "http://schemas.android.com/apk/res/android"

GENERATED_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />

    <!-- AndroidTV support -->
    <uses-feature android:name="android.software.leanback" android:required="false" />

    <application
        android:icon="@mipmap/ic_launcher"
        android:label="@string/app_name"
        android:theme="@style/Theme.ava"
        android:usesCleartextTraffic="true">
        <activity
            android:configChanges="orientation|keyboardHidden|keyboard|screenSize|locale|smallestScreenSize|screenLayout|uiMode"
            android:launchMode="singleTask"
            android:label="@string/main_activity_title"
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
                <category android:name="android.intent.category.LEANBACK_LAUNCHER" />
            </intent-filter>
        </activity>

        <provider
          android:name="androidx.core.content.FileProvider"
          android:authorities="com.ava.shell.fileprovider"
          android:exported="false"
          android:grantUriPermissions="true">
          <meta-data
            android:name="android.support.FILE_PROVIDER_PATHS"
            android:resource="@xml/file_paths" />
        </provider>
    </application>
</manifest>
"""

GENERATED_GRADLE = """import java.util.Properties

plugins {
    id("com.android.application")
}

android {
    compileSdk = 36
    buildTypes {
        getByName("debug") {
            isDebuggable = true
        }
        getByName("release") {
            isMinifyEnabled = true
        }
    }
}
"""


def _load_overlay():
    """Import the patcher by path — `ui/app/android/` is not an import package."""
    spec = importlib.util.spec_from_file_location("apply_overlay", _OVERLAY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


overlay = _load_overlay()


def _attr(element: ET.Element, name: str) -> str | None:
    return element.get(f"{{{_ANDROID_NS}}}{name}")


@pytest.fixture
def patched() -> ET.Element:
    # The source is the fixed generated-manifest fixture above, not untrusted XML.
    return ET.fromstring(overlay.patch_manifest(GENERATED_MANIFEST))  # noqa: S314


def test_adds_the_permissions_the_shell_needs(patched: ET.Element) -> None:
    names = {_attr(e, "name") for e in patched.findall("uses-permission")}
    assert set(overlay.PERMISSIONS) <= names
    # The template's own permission must survive the patch.
    assert "android.permission.INTERNET" in names


def test_declares_the_foreground_service(patched: ET.Element) -> None:
    application = patched.find("application")
    assert application is not None
    services = [
        e for e in application.findall("service") if _attr(e, "name") == overlay.SERVICE_NAME
    ]
    assert len(services) == 1
    assert _attr(services[0], "foregroundServiceType") == overlay.SERVICE_TYPE
    # A service another app can start is a service another app can abuse.
    assert _attr(services[0], "exported") == "false"
    properties = services[0].findall("property")
    assert len(properties) == 1
    assert _attr(properties[0], "name") == overlay.SPECIAL_USE_PROPERTY
    assert _attr(properties[0], "value") == overlay.SPECIAL_USE_DESCRIPTION


def test_points_the_application_at_the_network_security_config(patched: ET.Element) -> None:
    application = patched.find("application")
    assert application is not None
    assert _attr(application, "networkSecurityConfig") == "@xml/network_security_config"
    # The blanket flag and the config would be two answers to one question.
    assert _attr(application, "usesCleartextTraffic") is None


def test_keeps_the_template_activity_and_provider(patched: ET.Element) -> None:
    application = patched.find("application")
    assert application is not None
    assert [_attr(e, "name") for e in application.findall("activity")] == [".MainActivity"]
    assert len(application.findall("provider")) == 1


def test_is_idempotent() -> None:
    """The workflow may re-run the overlay on an already-patched tree; a second
    pass must not stack duplicate permissions or a second <service>."""
    once = overlay.patch_manifest(GENERATED_MANIFEST)
    twice = overlay.patch_manifest(once)
    assert once == twice


def test_gradle_overlay_reads_ci_signing_properties_only_when_present() -> None:
    patched = overlay.patch_gradle(GENERATED_GRADLE)
    assert "import java.io.FileInputStream" in patched
    assert 'rootProject.file("keystore.properties")' in patched
    assert 'create("release")' in patched
    assert 'signingConfig = signingConfigs.getByName("release")' in patched
    assert "if (keystorePropertiesFile.exists())" in patched


def test_gradle_overlay_is_idempotent() -> None:
    once = overlay.patch_gradle(GENERATED_GRADLE)
    assert overlay.patch_gradle(once) == once


def test_gradle_overlay_rejects_an_unknown_template() -> None:
    with pytest.raises(ValueError):
        overlay.patch_gradle("plugins {}\n")


def test_rejects_a_manifest_it_does_not_recognise() -> None:
    """Failing loudly beats shipping an APK whose background residency was
    silently never wired up."""
    with pytest.raises(ValueError):
        overlay.patch_manifest("<manifest/>")
    with pytest.raises(ValueError):
        overlay.patch_manifest("<something-else/>")


def test_the_kotlin_sources_match_the_package_the_patcher_installs_into() -> None:
    """`register_android_plugin` looks the class up by fully qualified name, so
    the Kotlin `package` line, the patcher's install path and the Rust constant
    have to agree; a mismatch only shows up as a runtime failure on a device."""
    java_dir = _OVERLAY.parent / "java"
    sources = sorted(java_dir.glob("*.kt"))
    assert sources, "the overlay must carry the Kotlin sources"
    for source in sources:
        assert f"package {overlay.PACKAGE}" in source.read_text()

    rust = (_REPO_ROOT / "ui" / "app" / "src-tauri" / "src" / "android.rs").read_text()
    assert f'PLUGIN_PACKAGE: &str = "{overlay.PACKAGE}"' in rust
