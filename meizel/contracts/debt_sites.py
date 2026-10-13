"""`debt sites`: every place in the source that a code that names a target is reported.

The baselines count violations per file and key; they do not say where. A codemod that removes
one kind of patch, and a design that needs the list of its sites, ask the source: this scans the
files and lists each patch with its file, line, form and target, optionally for the targets under
some prefixes. It reports what the source holds now, whether or not the baseline records it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from meizel.contracts.analysis import scan_all
from meizel.contracts.debt import PATCH_CODES, target_under
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import Universe

__all__ = ["Site", "format_sites", "sites"]


@dataclass(frozen=True, slots=True)
class Site:
    path: str
    line: int
    code: str
    form: str
    target: str


def _code(name: str) -> Code:
    codes = ", ".join(c.value for c in PATCH_CODES)
    for code in PATCH_CODES:
        if code.value == name:
            return code
    raise MeizelError(f"{name!r}: no sites are listed for this code; the codes are {codes}")


def _within(path: str, scopes: list[str]) -> bool:
    return not scopes or any(path == s or path.startswith(f"{s.rstrip('/')}/") for s in scopes)


def sites(
    universe: Universe,
    code: str,
    prefixes: list[str],
    paths: list[str],
    jobs: int | None = None,
) -> list[Site]:
    """The sites of `code` whose target is under one of the prefixes (any, when none is given).

    Sorted by target, then file and line, so the same tree always lists the same way.
    """
    wanted = _code(code)
    scopes = [p.rstrip("/") for p in paths]
    files = [p for p in universe.python_files if _within(p, scopes)]
    found: list[Site] = []
    for path, scan in sorted(scan_all(universe, jobs, files).items()):
        found.extend(
            Site(path, patch.line, wanted.value, patch.form, patch.target)
            for patch in scan.patches
            if patch.code is wanted
            and (not prefixes or any(target_under(patch.target, prefix) for prefix in prefixes))
        )
    return sorted(found, key=lambda s: (s.target, s.path, s.line, s.form))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_sites(found: list[Site], *, as_json: bool) -> str:
    if as_json:
        return json.dumps([asdict(site) for site in found], indent=2)
    lines = [f"{s.path}:{s.line}: {s.form} {s.target}" for s in found]
    files = len({s.path for s in found})
    lines.append(f"meizel: {_count(len(found), 'site')} in {_count(files, 'file')}")
    return "\n".join(lines)
