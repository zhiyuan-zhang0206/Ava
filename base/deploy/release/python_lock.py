"""Dependency-free canonical lock validation shared by bootstrap and CI."""

from __future__ import annotations

import tomllib
from pathlib import Path

_INDEX = "https://pypi.org/simple"
_ARTIFACT_PREFIX = "https://files.pythonhosted.org/packages/"


def violations(path: Path) -> list[str]:
    """Report registry packages whose index or artifact transport is host-specific."""
    with path.open("rb") as stream:
        lock = tomllib.load(stream)
    errors: list[str] = []
    for package in lock["package"]:
        source = package["source"]
        if "registry" not in source:
            continue
        name = package["name"]
        if source["registry"] != _INDEX:
            errors.append(f"{name}: registry must be {_INDEX}")
        artifacts = list(package.get("wheels", []))
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if any(not item["url"].startswith(_ARTIFACT_PREFIX) for item in artifacts):
            errors.append(f"{name}: distribution URLs must use {_ARTIFACT_PREFIX}")
    return errors
