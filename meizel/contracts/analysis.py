"""Whole-repository analysis: facts, projections and every diagnostic."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from meizel.contracts.debt import file_key
from meizel.contracts.ledger import reconcile
from meizel.contracts.manifest import Manifest, load_manifests, manifest_path
from meizel.contracts.policy import check as check_policy
from meizel.contracts.policy import load_policy
from meizel.contracts.rule import Projection, judge, project, relative
from meizel.graph.model import Code, DebtKey, Diagnostic, Fact, FactKind
from meizel.graph.universe import Universe, load_universe
from meizel.language.fixtures import FixtureIndex, fixture_facts
from meizel.language.scan import EXTERNAL_FORMS, FileScan, scan_file

__all__ = ["Analysis", "analyze", "diagnose", "scan_all"]

_PARALLEL_FROM = 200
_WORKER: dict[str, Universe] = {}


@dataclass(slots=True)
class Analysis:
    """Everything one run found.

    `diagnostics` holds every violation the rules found. `verdict` holds the ones that fail
    `check` once each directory's recorded debt is taken into account. `changed` is the set of
    files a `check --changed` run judged, or None for a whole-repository run.
    """

    universe: Universe
    manifests: dict[str, Manifest]
    scans: dict[str, FileScan]
    facts: list[Fact]
    projections: list[Projection]
    diagnostics: list[Diagnostic] = field(default_factory=list[Diagnostic])
    verdict: list[Diagnostic] = field(default_factory=list[Diagnostic])
    changed: frozenset[str] | None = None
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


def scan_all(
    universe: Universe, jobs: int | None = None, paths: Sequence[str] | None = None
) -> dict[str, FileScan]:
    paths = universe.python_files if paths is None else paths
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


def _fixture_facts(
    universe: Universe, scans: dict[str, FileScan], judged: Collection[str]
) -> list[Fact]:
    defined = {
        path: scan.fixtures.defs | scan.imported
        for path, scan in scans.items()
        if scan.fixtures.defs or path.endswith("conftest.py") or scan.fixtures.plugins
    }
    plugins = {
        p: [n for n, _ in s.fixtures.plugins] for p, s in scans.items() if s.fixtures.plugins
    }
    requests = {
        p: s.fixtures.requests for p, s in scans.items() if s.fixtures.requests and p in judged
    }
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
        owner = scan.path.rpartition("/")[0]
        for item in scan.restricted:
            if scan.path in externals and item.form in EXTERNAL_FORMS:
                continue
            form = f"{item.form}{' ' + repr(item.detail) if item.detail else ''}"
            found.append(
                Diagnostic(
                    scan.path,
                    item.line,
                    Code.RESTRICTED,
                    f"{form} is outside the restricted Python subset",
                    (
                        "use a static import, and inject a collaborator instead of patching it; a file that genuinely "
                        f"owns a dynamic load declares it under [loads] in {manifest_path(scan.path.rpartition('/')[0])}; "
                        "a file that runs code from outside the repository declares it under [external] with a reason",
                    ),
                    DebtKey(owner, file_key(owner, scan.path, form)),
                )
            )
    return found


_PATCH_WHAT = {
    Code.FIRST_PARTY_PATCH: "first-party",
    Code.STDLIB_PATCH: "standard-library",
    Code.THIRD_PARTY_PATCH: "third-party",
    Code.ENV_PATCH: "the environment",
    Code.UNRESOLVED_PATCH: "an unresolved target",
}
_PATCH_HINT = {
    Code.FIRST_PARTY_PATCH: (
        "do not patch first-party code: split the code under test into a public facade and an "
        "implementation whose dependencies are explicit parameters, and let the test pass the "
        "fake in directly"
    ),
    Code.STDLIB_PATCH: (
        "do not patch the standard library: the code under test takes the dependency (a clock, a "
        "sleeper, a file opener) as an explicit parameter, and the test passes the fake in directly"
    ),
    Code.THIRD_PARTY_PATCH: (
        "do not patch a third-party package: wrap it behind a first-party seam whose dependency is "
        "an explicit parameter, and let the test pass the fake in directly"
    ),
    Code.ENV_PATCH: (
        "do not patch the environment: read the setting once at the boundary and pass the value "
        "in as an explicit parameter"
    ),
    Code.UNRESOLVED_PATCH: (
        "the target of this patch is not an imported module or object, so what it replaces is "
        "invisible to the checker: inject the dependency as an explicit parameter instead of "
        "patching it"
    ),
}


def _patch_diagnostics(scans: Iterable[FileScan]) -> list[Diagnostic]:
    """Patching: the dependency was not injected."""
    found: list[Diagnostic] = []
    for scan in scans:
        owner = scan.path.rpartition("/")[0]
        for patch in scan.patches:
            found.append(
                Diagnostic(
                    scan.path,
                    patch.line,
                    patch.code,
                    f"{patch.form} on {_PATCH_WHAT[patch.code]} {patch.target}",
                    (_PATCH_HINT[patch.code],),
                    DebtKey(owner, file_key(owner, scan.path, f"{patch.form} {patch.target}")),
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


@dataclass(slots=True)
class StaleDeclarations:
    """Declared edges, loads and externals that nothing needs; only a whole run knows them."""

    edges: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    loads: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    externals: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def _stale_diagnostics(
    manifests: dict[str, Manifest], stale: StaleDeclarations
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for directory, cf, ct in stale.edges:
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
    for directory, name in stale.loads:
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
    for directory, name in stale.externals:
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


def _scan_changed(
    universe: Universe, changed: Collection[str], jobs: int | None
) -> dict[str, FileScan]:
    """The changed Python files, plus what resolving their fixture requests needs."""
    wanted = {p for p in universe.python_files if p in changed or p.endswith("conftest.py")}
    scans = scan_all(universe, jobs, sorted(wanted))
    while True:
        modules = (
            universe.resolve_module(n) for s in scans.values() for n, _ in s.fixtures.plugins
        )
        more = sorted({m.file for m in modules if m is not None and m.file and m.file not in scans})
        if not more:
            return scans
        scans.update(scan_all(universe, jobs, more))


def _distinct(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """One diagnostic per site and message, as `check` prints them."""
    return list({(d.path, d.line, d.code, d.message): d for d in diagnostics}.values())


def _judge_facts(
    facts: list[Fact], manifests: dict[str, Manifest]
) -> tuple[list[Projection], list[Diagnostic], dict[tuple[str, str, str], list[Fact]]]:
    """Project every fact edge and judge it against the manifests."""
    projections = [p for p in map(project, facts) if p is not None]
    found: list[Diagnostic] = []
    needed: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for proj in projections:
        found.extend(judge(proj, manifests))
        manifest = manifests.get(proj.lca)
        if not proj.exempt and manifest is not None:
            for source, target in manifest.covering(*relative(proj)):
                needed[(proj.lca, source, target)].append(proj.fact)
    return projections, found, dict(needed)


def diagnose(scan: FileScan, manifests: dict[str, Manifest]) -> list[Diagnostic]:
    """The violations one scanned file contributes by itself, as `analyze` finds them."""
    _, found, _ = _judge_facts(scan.facts, manifests)
    loaders, externals = _declared_loads(manifests), _declared_externals(manifests)
    found.extend(_source_diagnostics([scan], loaders, externals))
    found.extend(_patch_diagnostics([scan]))
    return _distinct(found)


def analyze(
    root: Path, *, jobs: int | None = None, changed: Collection[str] | None = None
) -> Analysis:
    """Analyse the repository, or with `changed` only the facts those files contribute.

    A partial run still reads every manifest, so the declared graphs are checked in full. It
    judges the changed files' fact edges, restricted forms and recorded debt, and every `[loads]`
    declaration, but not what is stale: that needs the whole repository.
    """
    universe = load_universe(root)
    manifests, diagnostics = load_manifests(universe)
    policy, policy_problems = load_policy(universe)
    diagnostics.extend(policy_problems)
    diagnostics.extend(check_policy(manifests, policy))
    scope = None if changed is None else frozenset(changed)
    scans = scan_all(universe, jobs) if scope is None else _scan_changed(universe, scope, jobs)
    judged = sorted(scans if scope is None else scope & scans.keys())
    loaders = _declared_loads(manifests)
    externals = _declared_externals(manifests)
    facts = [f for p in judged for f in scans[p].facts]
    facts.extend(_fixture_facts(universe, scans, judged))
    facts.extend(_load_facts(manifests))
    projections, found, needed = _judge_facts(facts, manifests)
    diagnostics.extend(found)
    diagnostics.extend(_source_diagnostics([scans[p] for p in judged], loaders, externals))
    diagnostics.extend(_patch_diagnostics(scans[p] for p in judged))
    stale = StaleDeclarations()
    if scope is None:
        stale = StaleDeclarations(
            _stale_edges(manifests, needed),
            _stale_loads(scans, loaders),
            _stale_externals(scans, externals),
        )
        diagnostics.extend(_stale_diagnostics(manifests, stale))
    diagnostics = _distinct(diagnostics)
    return Analysis(
        universe,
        manifests,
        scans,
        facts,
        projections,
        diagnostics,
        reconcile(diagnostics, manifests, lambda path: scope is None or path in scope),
        scope,
        needed,
        stale.edges,
        stale.loads,
        stale.externals,
    )
