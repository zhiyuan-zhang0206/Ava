"""The import statements of the non-test source, cached per file.

Placement (`scripts/structure/placement.py`) needs the direct import edges of the production
code: which package's subtree imports which module. Reading them means parsing ~1500 files
(1.3-2.5 s), too slow for a per-file pre-commit run, so each file's import statements are
kept in `.cache/structure/production-imports.json`, keyed on the file's `(mtime_ns, size)`.
A changed file re-parses itself and a deleted one drops out, so the cache can never be stale
and is never committed.

What is cached is the file's own text only: its first-party import statements with relative
imports made absolute. Which name in `from a import b` is a submodule depends on the rest of
the tree, so that resolution is not cached; the caller resolves the statements against the
current checkout with the placement rule's own import parsing (`collect_references`).

The scope is that of `UnitGraph.empirical`: every `.py` under the code tops except files
below a `tests` or `docs` directory.
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scripts.structure import locality

CACHE_PATH = ".cache/structure/production-imports.json"
_VERSION = 1
_SKIPPED_DIRS = frozenset({"tests", "docs", "__pycache__"})

Entry = tuple[int, int, str]  # (mtime_ns, size, import statements, one per line)


def production_imports(repo_root: Path, tops: Sequence[str]) -> dict[str, str]:
    """Repo-relative path -> the file's first-party import statements (one per line)."""
    cache_file = repo_root / CACHE_PATH
    cached = _load(cache_file)
    found: dict[str, Entry] = {}
    for rel, path in _source_files(repo_root, tops):
        stat = path.stat()
        hit = cached.get(rel)
        if hit is not None and hit[:2] == (stat.st_mtime_ns, stat.st_size):
            found[rel] = hit
        else:
            found[rel] = (stat.st_mtime_ns, stat.st_size, _statements(path, rel, tops))
    if found != cached:
        _store(cache_file, found)
    return {rel: entry[2] for rel, entry in found.items()}


def _source_files(repo_root: Path, tops: Sequence[str]) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for top in tops:
        for directory, subdirs, names in os.walk(repo_root / top):
            subdirs[:] = sorted(d for d in subdirs if d not in _SKIPPED_DIRS)
            for name in sorted(names):
                if name.endswith(".py"):
                    path = Path(directory, name)
                    files.append((path.relative_to(repo_root).as_posix(), path))
    return files


def _statements(path: Path, rel: str, tops: Sequence[str]) -> str:
    """The absolute first-party import statements of one file, function-level ones included."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return ""  # unparsable members are skipped, as every structure lint skips them
    lines: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in tops for alias in node.names):
                lines.append(ast.unparse(node))
        elif isinstance(node, ast.ImportFrom):
            base = locality._import_base(node, rel)
            if base and base.split(".")[0] in tops:
                lines.append(ast.unparse(ast.ImportFrom(module=base, names=node.names, level=0)))
    return "\n".join(lines)


def _load(cache_file: Path) -> dict[str, Entry]:
    """The cached entries; a missing, unreadable or foreign-version cache is simply empty."""
    try:
        data = cast("dict[str, object]", json.loads(cache_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    files = data.get("files")
    if data.get("version") != _VERSION or not isinstance(files, dict):
        return {}
    entries = cast("dict[str, Entry]", files)  # JSON arrays; a wrong shape is a cache miss
    return {rel: (entry[0], entry[1], entry[2]) for rel, entry in entries.items()}


def _store(cache_file: Path, entries: dict[str, Entry]) -> None:
    """Replace the cache atomically; a checkout that cannot be written just runs uncached."""
    payload = json.dumps({"version": _VERSION, "files": entries}, separators=(",", ":"))
    temporary = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(cache_file)
    except OSError:
        temporary.unlink(missing_ok=True)
