"""The uv version every install path uses comes from one canonical source.

``shared.brew_pin`` (UV_VERSION / UV_ASSET_SHA256) is canonical. toolchain.sh
embeds the same values because it runs before Python exists on a fresh box, the
CI workflows pin setup-uv with the same version, and the Windows setup guide
(conventions/windows-setup.md) embeds the Windows zip hashes; these tests fail
when any of the copies drift.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from shared import brew_pin

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLCHAIN = _REPO_ROOT / "scripts" / "provision" / "toolchain.sh"
_CI_WORKFLOWS = (
    _REPO_ROOT / ".github" / "workflows" / "ci.yml",
    _REPO_ROOT / ".github" / "workflows" / "update-model-pricing.yml",
    _REPO_ROOT / ".github" / "workflows" / "audit-branch-protection.yml",
)

_WINDOWS_SETUP = _REPO_ROOT / "conventions" / "windows-setup.md"

# One `echo "<platform-tag> <sha256>" ;;` arm per supported platform.
_TAG_SHA_ARM = re.compile(r'^\s*echo "([a-z0-9_-]+) ([0-9a-f]{64})"\s*;;', re.MULTILINE)


def test_toolchain_embeds_the_canonical_version() -> None:
    text = _TOOLCHAIN.read_text(encoding="utf-8")
    match = re.search(r'^UV_VERSION="([0-9][0-9.]*)"', text, re.MULTILINE)
    assert match is not None, "toolchain.sh must define UV_VERSION"
    assert match.group(1) == brew_pin.UV_VERSION


def test_toolchain_sha256_map_matches_the_canonical_set() -> None:
    text = _TOOLCHAIN.read_text(encoding="utf-8")
    embedded = dict(_TAG_SHA_ARM.findall(text))
    assert embedded == brew_pin.UV_ASSET_SHA256


def test_ci_setup_uv_pins_the_canonical_version() -> None:
    for workflow in _CI_WORKFLOWS:
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "astral-sh/setup-uv" not in line:
                continue
            version_line = next(
                candidate
                for candidate in lines[index + 1 :]
                if candidate.strip().startswith("version:")
            )
            pinned = version_line.split(":", 1)[1].strip().strip("'\"")
            assert pinned == brew_pin.UV_VERSION, (
                f"{workflow.name} pins setup-uv version {pinned!r}, "
                f"expected {brew_pin.UV_VERSION!r}"
            )


def test_toolchain_script_parses() -> None:
    subprocess.run(  # noqa: S603 — fixed argv executes the repository script
        ["bash", "-n", str(_TOOLCHAIN)], check=True
    )


def test_windows_setup_pins_the_canonical_version() -> None:
    text = _WINDOWS_SETUP.read_text(encoding="utf-8")
    match = re.search(r'^\s*\$v = "([0-9][0-9.]*)"', text, re.MULTILINE)
    assert match is not None, "windows-setup.md must define $v (the pinned uv version)"
    assert match.group(1) == brew_pin.UV_VERSION


def test_windows_setup_sha256_matches_the_canonical_set() -> None:
    text = _WINDOWS_SETUP.read_text(encoding="utf-8")
    embedded = set(re.findall(r"[0-9a-f]{64}", text))
    assert embedded, "windows-setup.md must embed the pinned uv asset sha256 values"
    canonical = set(brew_pin.UV_WINDOWS_ASSET_SHA256.values())
    assert embedded <= canonical, (
        "windows-setup.md embeds sha256 values not in brew_pin.UV_WINDOWS_ASSET_SHA256: "
        f"{embedded - canonical}"
    )
