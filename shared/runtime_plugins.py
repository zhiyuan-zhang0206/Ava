"""Declared plugin closure without importing mutable input code.

Installation/scaffold destinations remain mutable unit paths. Only discovery
consumes retained code. Legacy source-mode fail-soft behavior is unchanged.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

from shared import skill_names
from shared.plugin_manifest import check_host_engine, load_manifest, range_allows
from shared.runtime_release import ReleaseRejectedError


def declared_plugins(root: Path) -> dict[str, str]:
    """Reject half installs and unknown manifests; return declared versions."""
    versions: dict[str, str] = {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            raise ReleaseRejectedError("plugin input contains incomplete or unknown members")
        manifest = load_manifest(directory)
        if manifest is None or not (directory / "plugin.py").is_file():
            raise ReleaseRejectedError("retained plugin requires a complete declared package")
        if (directory / ".mcp.json").exists():
            raise ReleaseRejectedError("plugin MCP executable closure is not yet supported")
        if skill_names.match_key(manifest.name) != skill_names.match_key(directory.name):
            raise ReleaseRejectedError("plugin manifest identity differs from directory")
        for path in directory.rglob("*"):
            if path.name == ".git" or path.name.startswith(".env"):
                raise ReleaseRejectedError("plugin input contains checkout or secret configuration")
        if skill_names.find(directory.name, versions) is not None:
            raise ReleaseRejectedError("plugin names collide after normalization")
        versions[directory.name] = manifest.version
    return versions


def verify_plugin_dependencies(root: Path, required: tuple[str, ...]) -> None:
    """Validate against installed wheel dependencies, never host packages."""
    versions = declared_plugins(root)
    builtin = Path(__file__).resolve().parent.parent / "ava_builtins/plugins"
    if {skill_names.match_key(name) for name in versions} & {
        skill_names.match_key(path.name) for path in builtin.iterdir() if path.is_dir()
    }:
        raise ReleaseRejectedError("external plugin conflicts with builtin image package")
    if not set(required) <= set(versions):
        raise ReleaseRejectedError("required external plugin is missing from candidate")
    for name in versions:
        manifest = load_manifest(root / name)
        if manifest is None:
            raise ReleaseRejectedError("plugin manifest disappeared")
        if check_host_engine(manifest, importlib.metadata.version("ava")):
            raise ReleaseRejectedError("plugin requires another Ava version")
        if manifest.dependencies.host_capabilities:
            raise ReleaseRejectedError("plugin host capability closure is not yet supported")
        for dependency, constraint in manifest.dependencies.plugins.items():
            resolved = skill_names.find(dependency, versions)
            if resolved is None or not range_allows(constraint, versions[resolved]):
                raise ReleaseRejectedError("plugin dependency missing or incompatible")
        for package, constraint in manifest.dependencies.python_packages.items():
            if not range_allows(constraint, importlib.metadata.version(package)):
                raise ReleaseRejectedError("plugin Python dependency does not match locked image")
