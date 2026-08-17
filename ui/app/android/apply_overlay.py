#!/usr/bin/env python3
"""Apply the shell's Android customisations onto a freshly generated project.

`cargo tauri android init` writes `src-tauri/gen/android/` from the Tauri
version's own templates. That tree is a build output, not source: committing it
would freeze a generated Gradle project against one Tauri version and make every
upgrade a merge. So the release workflow regenerates it and this script layers
the three things the shell needs on top:

  1. the Kotlin foreground service + its Tauri plugin (`java/`);
  2. the network security config (`network_security_config.xml`);
  3. the manifest edits that make (1) and (2) take effect — permissions, the
     `<service>` declaration, and the `android:networkSecurityConfig` attribute.

Only (3) is delicate, because it edits a generated file. It is written to be
idempotent (re-running changes nothing) and to fail loudly if the generated
manifest stops looking the way it does today, rather than silently producing an
APK with no background residency.

Usage:
    python ui/app/android/apply_overlay.py [--gen-dir src-tauri/gen/android]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)

#: Java package the app is generated into — derived from `identifier` in
#: tauri.conf.json, and hard-coded in the Kotlin sources' `package` line.
PACKAGE = "com.ava.shell"

#: Permissions the shell adds on top of the template's INTERNET.
#:
#: FOREGROUND_SERVICE_SPECIAL_USE is the API 34+ typed companion to
#: FOREGROUND_SERVICE. The shell's persistent, user-visible SSE connection is
#: not media, location, a bounded data sync, or any other named service type;
#: Android reserves specialUse for exactly that gap. POST_NOTIFICATIONS is the
#: API 33+ runtime grant the notification bridge needs.
PERMISSIONS = (
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.FOREGROUND_SERVICE",
    "android.permission.FOREGROUND_SERVICE_SPECIAL_USE",
)

#: The foreground service, declared relative to the app package.
SERVICE_NAME = ".AvaBackgroundService"
SERVICE_TYPE = "specialUse"
SPECIAL_USE_PROPERTY = "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
SPECIAL_USE_DESCRIPTION = "Persistent user-enabled Ava agent event connection"

_GRADLE_SIGNING_MARKER = "// Ava release signing overlay."


def _android(attr: str) -> str:
    """Attribute name in the Android namespace, ElementTree's `{ns}name` form."""
    return f"{{{ANDROID_NS}}}{attr}"


def patch_manifest(manifest_xml: str) -> str:
    """Return `manifest_xml` with the shell's permissions, service and network
    security config applied. Idempotent.

    Raises `ValueError` when the manifest does not have the shape the Tauri
    template produces — an unrecognised manifest means the assumptions here are
    stale, and a silently unpatched manifest is the failure this guards.
    """
    # Input is Tauri's locally generated build file, never network/user XML.
    root = ET.fromstring(manifest_xml)  # noqa: S314
    if root.tag != "manifest":
        raise ValueError(f"expected a <manifest> root, found <{root.tag}>")
    application = root.find("application")
    if application is None:
        raise ValueError("the manifest has no <application> element to patch")

    existing = {element.get(_android("name")) for element in root.findall("uses-permission")}
    for permission in PERMISSIONS:
        if permission in existing:
            continue
        # Permissions belong before <application>; ElementTree keeps document
        # order, so insert rather than append.
        element = ET.Element("uses-permission", {_android("name"): permission})
        root.insert(len(root.findall("uses-permission")) + 1, element)

    application.set(_android("networkSecurityConfig"), "@xml/network_security_config")
    # The network security config is the finer-grained statement; leaving the
    # blanket flag next to it would just be a second, contradictory answer.
    application.attrib.pop(_android("usesCleartextTraffic"), None)

    service = next(
        (
            element
            for element in application.findall("service")
            if element.get(_android("name")) == SERVICE_NAME
        ),
        None,
    )
    if service is None:
        service = ET.SubElement(
            application,
            "service",
            {
                _android("name"): SERVICE_NAME,
                _android("exported"): "false",
                _android("foregroundServiceType"): SERVICE_TYPE,
            },
        )
    service.set(_android("exported"), "false")
    service.set(_android("foregroundServiceType"), SERVICE_TYPE)
    properties = {element.get(_android("name")): element for element in service.findall("property")}
    if SPECIAL_USE_PROPERTY not in properties:
        ET.SubElement(
            service,
            "property",
            {
                _android("name"): SPECIAL_USE_PROPERTY,
                _android("value"): SPECIAL_USE_DESCRIPTION,
            },
        )
    else:
        properties[SPECIAL_USE_PROPERTY].set(_android("value"), SPECIAL_USE_DESCRIPTION)

    ET.indent(root, space="    ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def patch_gradle(build_gradle: str) -> str:
    """Wire Tauri's generated Android app to an optional CI keystore.

    Tauri intentionally leaves release signing out of its generated Gradle
    file; merely writing ``keystore.properties`` does nothing. This patch is
    the documented Tauri 2 signing integration, guarded by file existence so a
    checkout with no signing secrets still produces an unsigned APK.
    """
    if _GRADLE_SIGNING_MARKER in build_gradle:
        return build_gradle
    required = ("import java.util.Properties", "    buildTypes {", 'getByName("release") {')
    missing = [needle for needle in required if needle not in build_gradle]
    if missing:
        raise ValueError(f"unrecognised Tauri Gradle template; missing {missing}")

    patched = build_gradle.replace(
        "import java.util.Properties",
        "import java.io.FileInputStream\nimport java.util.Properties",
        1,
    )
    signing = f"""    {_GRADLE_SIGNING_MARKER}
    signingConfigs {{
        create("release") {{
            val keystorePropertiesFile = rootProject.file("keystore.properties")
            if (keystorePropertiesFile.exists()) {{
                val keystoreProperties = Properties().apply {{
                    load(FileInputStream(keystorePropertiesFile))
                }}
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["password"] as String
                storeFile = file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["password"] as String
            }}
        }}
    }}

"""
    patched = patched.replace("    buildTypes {", signing + "    buildTypes {", 1)
    return patched.replace(
        '        getByName("release") {',
        """        getByName("release") {
            if (rootProject.file("keystore.properties").exists()) {
                signingConfig = signingConfigs.getByName("release")
            }""",
        1,
    )


def apply(overlay_dir: Path, gen_dir: Path) -> None:
    """Copy the Kotlin + resource overlay into `gen_dir` and patch its manifest."""
    main = gen_dir / "app" / "src" / "main"
    if not main.is_dir():
        raise SystemExit(
            f"{main} does not exist — run `cargo tauri android init` before this script"
        )

    package_dir = main / "java" / Path(*PACKAGE.split("."))
    package_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((overlay_dir / "java").glob("*.kt")):
        shutil.copy2(source, package_dir / source.name)

    xml_dir = main / "res" / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        overlay_dir / "network_security_config.xml",
        xml_dir / "network_security_config.xml",
    )

    manifest = main / "AndroidManifest.xml"
    manifest.write_text(patch_manifest(manifest.read_text(encoding="utf-8")) + "\n")

    gradle = gen_dir / "app" / "build.gradle.kts"
    gradle.write_text(patch_gradle(gradle.read_text(encoding="utf-8")), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    overlay_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gen-dir",
        type=Path,
        default=overlay_dir.parent / "src-tauri" / "gen" / "android",
        help="the generated Android project (default: ui/app/src-tauri/gen/android)",
    )
    args = parser.parse_args(argv)
    apply(overlay_dir, args.gen_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
