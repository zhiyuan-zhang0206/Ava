"""Install-time manifest gate shared by the install/upgrade entries (S1-S2).

Each install entry calls `manifest_install_errors` on the source tree it is
about to land. Empty list = nothing to refuse: no manifest (legacy package,
unchanged paths), or a manifest that validates and whose dependency mirror
holds. Any error = the package ships a contract it breaks; the caller prints
and refuses the install before anything is landed.

The MCP install/upgrade entries additionally pass `mirror_pyproject=True`,
which runs the `dependencies.pythonPackages` mirror check against the
package's own `pyproject.toml` — the durable #1198 defense (user ruling
2026-08-13: hard enforcement).
"""

from __future__ import annotations

import sys
from pathlib import Path

from shared import paths, plugin_manifest


def manifest_install_errors(pkg_dir: Path, *, mirror_pyproject: bool = False) -> list[str]:
    """Manifest checks for a package about to be landed; [] = proceed."""
    try:
        manifest = plugin_manifest.load_manifest(pkg_dir)
    except plugin_manifest.ManifestError as e:
        return [str(e)]
    if manifest is None:
        return []

    errors: list[str] = []
    try:
        host = plugin_manifest.host_version_from_repo(paths.repo_root())
    except plugin_manifest.ManifestError as e:
        return [str(e)]
    errors += plugin_manifest.check_host_engine(manifest, host)

    if mirror_pyproject:
        pyproject = pkg_dir / "pyproject.toml"
        if pyproject.is_file():
            try:
                specs = plugin_manifest.pyproject_dependency_specs(pkg_dir)
            except plugin_manifest.ManifestError as e:
                errors.append(str(e))
            else:
                errors += plugin_manifest.check_python_packages(manifest, specs)
        elif manifest.dependencies.python_packages:
            errors.append(
                "manifest declares dependencies.pythonPackages but the package "
                "has no pyproject.toml"
            )
    return errors


def gate_refuses(pkg_dir: Path, *, command: str, mirror_pyproject: bool = False) -> bool:
    """Print the gate's errors under the command's prefix and return True when
    the package must be refused (so a caller can `return 1` on one line)."""
    errors = manifest_install_errors(pkg_dir, mirror_pyproject=mirror_pyproject)
    if not errors:
        return False
    for err in errors:
        print(f"[ava {command}] {err}", file=sys.stderr)
    return True
