"""Composition roots declared by the package that owns them.

A package that takes what it reads (configuration slices, ...) from its composition root
says so in an `ambient_roots.toml` in its own directory: a key per kind, the root modules
relative to that directory. The ambient-state rule reads them all together, so a package
joins by adding its own file, and a moved or deleted package carries its declaration along.

    settings = ["daemon.py"]   # the only modules here that may read the global `settings`
    db = ["daemon.py"]         # the only modules here that may call `Database.from_settings()`
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import cast

ROOTS_FILE = "ambient_roots.toml"
KINDS = frozenset({"settings", "db"})
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})


def package_roots(repo_root: Path, kind: str) -> dict[str, frozenset[str]]:
    """Package directory -> its root modules (repo-relative), for the packages declaring `kind`."""
    if kind not in KINDS:
        raise ValueError(f"unknown composition-root kind {kind!r}; known: {sorted(KINDS)}")
    found: dict[str, frozenset[str]] = {}
    for current, dirs, files in os.walk(repo_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIPPED_DIRS)
        if ROOTS_FILE not in files:
            continue
        package = Path(current).relative_to(repo_root).as_posix()
        declared = tomllib.loads((Path(current) / ROOTS_FILE).read_text(encoding="utf-8"))
        unknown = sorted(set(declared) - KINDS)
        if unknown:
            raise ValueError(f"{package}/{ROOTS_FILE}: unknown kind(s) {unknown}")
        declared_names = declared.get(kind)
        if declared_names is None:
            continue
        names = cast("list[str]", declared_names)
        if (
            not isinstance(declared_names, list)
            or not names
            or not all(isinstance(n, str) for n in names)
        ):
            raise ValueError(f"{package}/{ROOTS_FILE}: {kind} must be a non-empty list of modules")
        found[package] = frozenset(f"{package}/{name}" for name in names)
    return dict(sorted(found.items()))
