"""Whole-repository analysis: facts, projections and every diagnostic."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from meizel.contracts.manifest import Manifest, load_manifests, manifest_path
from meizel.contracts.rule import Projection, judge, project
from meizel.graph.model import Code, Diagnostic, Fact, FactKind
from meizel.graph.universe import Universe, load_universe
from meizel.language.fixtures import FixtureIndex, fixture_facts
from meizel.language.scan import EXTERNAL_FORMS, FileScan, scan_file

__all__ = ["Analysis", "analyze"]

_PARALLEL_FROM = 200
_WORKER: dict[str, Universe] = {}


@dataclass(slots=True)
class Analysis:
    universe: Universe
    manifests: dict[str, Manifest]
    scans: dict[str, FileScan]
    facts: list[Fact]
    projections: list[Projection]
    diagnostics: list[Diagnostic] = field(default_factory=list[Diagnostic])
    needed: dict[tuple[str, str, str], list[Fact]] = field(
        default_factory=dict[tuple[str, str, str], list[Fact]]
    )
    stale_edges: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    stale_loads: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    stale_externals: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def _init_worker(universe: Universe) -> None:
    _WORKER["universe"] = universe


def _scan_in_worker(path: str) -> FileScan:
    return scan_file(_WORKER["universe"], path)


def scan_all(universe: Universe, jobs: int | None = None) -> dict[str, FileScan]:
    paths = universe.python_files
    workers = jobs if jobs is not None else min(os.cpu_count() or 1, 8)
    if workers <= 1 or len(paths) < _PARALLEL_FROM:
        return {p: scan_file(universe, p) for p in paths}
    with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(universe,)) as pool:
        scans = pool.map(_scan_in_worker, paths, chunksize=max(1, len(paths) // (workers * 8)))
        return {scan.path: scan for scan in scans}


def _load_facts(manifests: dict[str, Manifest]) -> list[Fact]:
    facts: list[Fact] = []
    for directory, manifest in manifests.items():
        for name, targets in manifest.loads.items():
            src = f"{directory}/{name}" if directory else name
            facts.extend(
                Fact(src, target, manifest.line_of(name), FactKind.LOAD, (), f"[loads] {target}")
                for target in targets
            )
    return facts


def _fixture_facts(universe: Universe, scans: dict[str, FileScan]) -> list[Fact]:
    defined = {
        path: scan.fixtures.defs | scan.imported
        for path, scan in scans.items()
        if scan.fixtures.defs or path.endswith("conftest.py") or scan.fixtures.plugins
    }
    plugins = {
        p: [n for n, _ in s.fixtures.plugins] for p, s in scans.items() if s.fixtures.plugins
    }
    requests = {p: s.fixtures.requests for p, s in scans.items() if s.fixtures.requests}
    return fixture_facts(FixtureIndex(universe, defined, plugins), requests)


def _declared_loads(manifests: dict[str, Manifest]) -> set[str]:
    return {
        f"{directory}/{name}" if directory else name
        for directory, manifest in manifests.items()
        for name in manifest.loads
    }


def _declared_externals(manifests: dict[str, Manifest]) -> set[str]:
    return {
        f"{directory}/{name}" if directory else name
        for directory, manifest in manifests.items()
        for name in manifest.external
    }


def _source_diagnostics(
    scans: Iterable[FileScan], loaders: set[str], externals: set[str]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for scan in scans:
        found.extend(Diagnostic(scan.path, p.line, p.code, p.message) for p in scan.problems)
        if scan.path in loaders:
            continue
        for item in scan.restricted:
            if scan.path in externals and item.form in EXTERNAL_FORMS:
                continue
            found.append(
                Diagnostic(
                    scan.path,
                    item.line,
                    Code.RESTRICTED,
                    f"{item.form}{' ' + repr(item.detail) if item.detail else ''} is outside the restricted Python subset",
                    (
                        "use a static import or the object form of patching; a file that genuinely "
                        f"owns a dynamic load declares it under [loads] in {manifest_path(scan.path.rpartition('/')[0])}; "
                        "a file that runs code from outside the repository declares it under [external] with a reason",
                    ),
                )
            )
    return found


def _stale_loads(scans: dict[str, FileScan], loaders: set[str]) -> list[tuple[str, str]]:
    return sorted(
        (path.rpartition("/")[0], path.rpartition("/")[2])
        for path in loaders
        if path not in scans or not scans[path].restricted
    )


def _stale_externals(scans: dict[str, FileScan], externals: set[str]) -> list[tuple[str, str]]:
    return sorted(
        (path.rpartition("/")[0], path.rpartition("/")[2])
        for path in externals
        if path not in scans or not any(r.form in EXTERNAL_FORMS for r in scans[path].restricted)
    )


def _stale_edges(
    manifests: dict[str, Manifest], needed: dict[tuple[str, str, str], list[Fact]]
) -> list[tuple[str, str, str]]:
    return [
        (directory, cf, ct)
        for directory, manifest in sorted(manifests.items())
        for cf, targets in manifest.uses.items()
        for ct in targets
        if (directory, cf, ct) not in needed
    ]


def _stale_diagnostics(
    manifests: dict[str, Manifest],
    stale_edges: list[tuple[str, str, str]],
    stale_loads: list[tuple[str, str]],
    stale_externals: list[tuple[str, str]],
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for directory, cf, ct in stale_edges:
        manifest = manifests[directory]
        found.append(
            Diagnostic(
                manifest.path,
                manifest.line_of(cf),
                Code.MANIFEST_STALE_EDGE,
                f"{cf} -> {ct} is declared but no fact edge needs it",
                ("run `python -m meizel sync --prune` to remove it",),
            )
        )
    for directory, name in stale_loads:
        manifest = manifests[directory]
        found.append(
            Diagnostic(
                manifest.path,
                manifest.line_of(name),
                Code.STALE_LOAD,
                f"[loads] {name!r} is declared but the file has no dynamic load",
                ("run `python -m meizel sync --prune` to remove it",),
            )
        )
    for directory, name in stale_externals:
        manifest = manifests[directory]
        found.append(
            Diagnostic(
                manifest.path,
                manifest.line_of(name),
                Code.STALE_EXTERNAL,
                f"[external] {name!r} is declared but the file runs no external code",
                ("remove the entry",),
            )
        )
    return found


def analyze(root: Path, *, jobs: int | None = None) -> Analysis:
    universe = load_universe(root)
    manifests, diagnostics = load_manifests(universe)
    scans = scan_all(universe, jobs)
    loaders = _declared_loads(manifests)
    externals = _declared_externals(manifests)
    facts = [f for scan in scans.values() for f in scan.facts]
    facts.extend(_fixture_facts(universe, scans))
    facts.extend(_load_facts(manifests))
    projections = [p for p in map(project, facts) if p is not None]
    needed: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for proj in projections:
        diagnostics.extend(judge(proj, manifests))
        if not proj.exempt:
            needed[proj.local_edge].append(proj.fact)
    diagnostics.extend(_source_diagnostics(scans.values(), loaders, externals))
    stale_edges = _stale_edges(manifests, needed)
    stale_loads = _stale_loads(scans, loaders)
    stale_externals = _stale_externals(scans, externals)
    diagnostics.extend(_stale_diagnostics(manifests, stale_edges, stale_loads, stale_externals))
    return Analysis(
        universe,
        manifests,
        scans,
        facts,
        projections,
        diagnostics,
        dict(needed),
        stale_edges,
        stale_loads,
        stale_externals,
    )
