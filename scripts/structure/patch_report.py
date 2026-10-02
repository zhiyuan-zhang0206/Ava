"""The patch-target census behind `scripts/lint/patch_targets.py --report`.

Renders, as Markdown, the class distribution (A-E and U), the composition of class D
(violations) by the relation of the test's home to the owning package, the per-home table,
the production modules most often reached into privately (the injection-seam work list), and
the most-patched ambient modules. Never affects the exit code: it is the measurement a
package move or an injection refactor is judged by.
"""

from __future__ import annotations

import collections
from collections.abc import Mapping

from scripts.structure.patch_targets import FileResult, Site

_CLASSES = (
    ("A", "environment boundary (stdlib, third-party, runtime, non-AVA env vars)"),
    ("B", "own package (the owning package contains the test's home)"),
    ("C", "another package's public name"),
    ("D", "violation: a private name of a package the test does not belong to"),
    ("E", "global environment (settings, paths, identity, env resolution, ambient services)"),
    ("U", "unresolved (object not statically known)"),
)
_RELATIONS = (
    (
        "ancestor",
        "the test's home is a strict ancestor of the owner (patches a descendant's private)",
    ),
    ("other-unit", "the owner is in a different top-level unit"),
    ("sibling", "same unit, another lineage"),
    ("top-level", "the test has no package home"),
)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _sites(results: Mapping[str, FileResult]) -> list[tuple[str, FileResult, Site]]:
    return [(rel, result, site) for rel, result in sorted(results.items()) for site in result.sites]


def _distribution(sites: list[tuple[str, FileResult, Site]]) -> list[str]:
    count = collections.Counter(site.cat for _, _, site in sites)
    deep = sum(1 for _, _, site in sites if site.cat == "C" and site.deep)
    rows = [[cls, meaning, str(count[cls])] for cls, meaning in _CLASSES]
    rows.append(["", "total patch points", str(len(sites))])
    lines = ["## Classes", "", *_table(["class", "meaning", "points"], rows), ""]
    lines.extend(
        [f"Class C includes {deep} deep attributes (`module.Class.method`), not violations.", ""]
    )
    return lines


def _violations(sites: list[tuple[str, FileResult, Site]]) -> list[str]:
    violating = [(rel, site) for rel, _, site in sites if site.cat == "D"]
    by_relation = collections.Counter(site.relation for _, site in violating)
    keys = {f"{rel}::{site.key}" for rel, site in violating}
    files = {rel for rel, _ in violating}
    rows = [[name, meaning, str(by_relation[name])] for name, meaning in _RELATIONS]
    return [
        "## Class D by relation of the test's home to the owning package",
        "",
        f"{len(violating)} points, {len(keys)} distinct (file, target) keys, {len(files)} files.",
        "",
        *_table(["relation", "meaning", "points"], rows),
        "",
    ]


def _by_home(results: Mapping[str, FileResult], limit: int) -> list[str]:
    homes: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    files: collections.Counter[str] = collections.Counter()
    for result in results.values():
        home = result.home or "(top-level)"
        if result.sites:
            files[home] += 1
        homes[home].update(site.cat for site in result.sites)
    ranked = sorted(homes.items(), key=lambda item: (-sum(item[1].values()), item[0]))[:limit]
    rows = [
        [f"`{home}`", str(files[home]), str(sum(c.values())), *(str(c[cls]) for cls, _ in _CLASSES)]
        for home, c in ranked
    ]
    headers = ["test home", "files", "points", *(cls for cls, _ in _CLASSES)]
    return [f"## Patch points by test home (top {limit})", "", *_table(headers, rows), ""]


def _violations_by_home(results: Mapping[str, FileResult], limit: int) -> list[str]:
    count: collections.Counter[str] = collections.Counter()
    for result in results.values():
        count[result.home or "(top-level)"] += sum(1 for s in result.sites if s.cat == "D")
    rows = [[f"`{home}`", str(n)] for home, n in count.most_common(limit) if n]
    return [
        f"## Class D by test home (top {limit})",
        "",
        *_table(["test home", "D points"], rows),
        "",
    ]


def _modules(sites: list[tuple[str, FileResult, Site]], limit: int) -> list[str]:
    modules: dict[str, dict[str, collections.Counter[str]]] = {}
    for _, result, site in sites:
        if site.cat != "D":
            continue
        entry = modules.setdefault(
            site.module, {"homes": collections.Counter(), "keys": collections.Counter()}
        )
        entry["homes"][result.home or "(top-level)"] += 1
        entry["keys"][site.key.rsplit(".", 1)[-1]] += 1
    ranked = sorted(modules.items(), key=lambda item: (-sum(item[1]["keys"].values()), item[0]))
    rows = [
        [
            f"`{module}`",
            str(sum(entry["keys"].values())),
            ", ".join(f"`{name}` {n}" for name, n in entry["keys"].most_common(3)),
            ", ".join(f"`{home}` {n}" for home, n in entry["homes"].most_common(4)),
        ]
        for module, entry in ranked[:limit]
    ]
    headers = ["production module", "points", "private names", "patched from (test home)"]
    return [f"## Class D by production module (top {limit})", "", *_table(headers, rows), ""]


def _ambient(sites: list[tuple[str, FileResult, Site]], limit: int) -> list[str]:
    patches: collections.Counter[str] = collections.Counter()
    homes: dict[str, set[str]] = collections.defaultdict(set)
    tier: dict[str, str] = {}
    for _, result, site in sites:
        if site.cat == "E":
            patches[site.module] += 1
            homes[site.module].add(result.home or "(top-level)")
            tier[site.module] = site.sub
    rows = [
        [f"`{module}`", tier[module], str(n), str(len(homes[module]))]
        for module, n in patches.most_common(limit)
    ]
    headers = ["ambient module", "tier", "points", "test homes"]
    return [f"## Class E by module (top {limit})", "", *_table(headers, rows), ""]


def render(results: Mapping[str, FileResult], *, baseline_keys: int, baseline_points: int) -> str:
    """The Markdown census of `results` (repo-relative path -> analysed file)."""
    sites = _sites(results)
    fallback = sorted(rel for rel, result in results.items() if result.fallback)
    lines = [
        "# Patch-target census",
        "",
        f"{len(results)} test files with patch points scanned; frozen baseline: "
        f"{baseline_keys} keys, {baseline_points} sites.",
        "",
        *_distribution(sites),
        *_violations(sites),
        *_violations_by_home(results, 15),
        *_by_home(results, 15),
        *_modules(sites, 20),
        *_ambient(sites, 15),
        "## Files placed by the patch-evidence fallback",
        "",
        "Every strong first-party reference of these files is patch evidence, so they keep it "
        "(see `scripts/structure/placement.py`): "
        + (", ".join(f"`{rel}`" for rel in fallback) or "none")
        + ".",
        "",
    ]
    return "\n".join(lines)
