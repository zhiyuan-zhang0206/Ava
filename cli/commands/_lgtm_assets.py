"""Pinned native LGTM assets and executable arguments for supported hosts."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from shared.lgtm_local import NATIVE_SERVICES

_NATIVE_CONSTANTS = NATIVE_SERVICES

def load_versions(repo: Path, tag: str) -> dict[str, dict[str, str]]:
    """Read the same pinned versions with platform-specific verified archives."""
    raw = yaml.safe_load((repo / "deploy/lgtm/native/versions.yml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("deploy/lgtm/native/versions.yml must contain a mapping")
    entries = cast(dict[str, object], raw)
    versions: dict[str, dict[str, str]] = {}
    asset_tag = tag.replace("_", "-")
    for name, constants in _NATIVE_CONSTANTS.items():
        service = entries[name]
        if not isinstance(service, dict):
            raise TypeError(f"native LGTM version entry for {name} must be a mapping")
        values = cast(dict[str, object], service)
        version, assets = values["version"], values["assets"]
        if not isinstance(version, str) or not isinstance(assets, dict):
            raise TypeError(f"native LGTM version entry for {name} is invalid")
        asset = cast(dict[str, object], assets)[asset_tag]
        if not isinstance(asset, dict):
            raise TypeError(f"native LGTM {asset_tag} asset for {name} must be a mapping")
        data = cast(dict[str, object], asset)
        url, sha256 = data["url"], data["sha256"]
        if not isinstance(url, str) or not isinstance(sha256, str):
            raise TypeError(f"native LGTM {asset_tag} asset for {name} is invalid")
        member = constants.archive_member
        versions[name] = {
            "version": version,
            "url": url,
            "sha256": sha256,
            "member": member.replace("darwin-arm64", asset_tag) if member else "",
        }
    return versions
