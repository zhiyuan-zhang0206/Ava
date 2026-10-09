"""Patch targets: which package's private names a test replaces (structure Rule 8).

A test may replace a name in its own package, a public name anywhere, the process
environment, or a third-party / runtime boundary. It may not reach into a private name of a
package it does not belong to. This module classifies every patch point of a test file
(`scripts/structure/patch_points.py`) against the file's home package
(`scripts/structure/placement.py`). Existing exemptions in the `patch_targets`
baseline section use `path::target -> site count`, matched exactly like Rules 4 and 5.
New violations must be fixed; introducing the lint cannot freeze new exemptions.

Classes (each patch point lands in exactly one):

- **A boundary**: the target is not repository code (stdlib, third-party, the runtime, the
  `tests` harness), or an environment variable that is not an `AVA_*` setting.
- **E ambient**: a module of the process-global environment (settings, paths, machine and
  cluster identity, env resolution, ambient service singletons: `E_MODULES`), or an
  `AVA_*` environment variable. Tests replace these through the same handful of seams
  everywhere; their replacement belongs to a test-support design, not to a package door.
- **B own package**: repository code whose owning package contains the test's home
  (private names included: the owner may reach its own internals).
- **C public entry**: another package's public name (a deep attribute of it is counted
  separately as `deep`).
- **D violation**: a private name (leading underscore attribute, or a private module
  segment) whose owning package does not contain the test's home. The owner is resolved as
  Rule 4 resolves it (`locality._private_target`): the package that owns the first private
  component. The relation of the home to the owner is one of `ancestor` (the home is a strict
  ancestor: the test spans several packages and reaches into one of them), `other-unit`,
  `sibling` (same unit, another lineage) or `top-level` (a test with no package home).
- **U unresolved**: the patched object cannot be resolved statically (a parameter, a call
  result). Counted, never a violation.

Only D is a violation. Baseline growth is a violation; shrinkage fails until the entry is
lowered or removed (`locality.site_errors` semantics); against the base revision the
section is shrink-only with `git -M` renames carried (`scripts/lint/code_structure.py`).
"""

from __future__ import annotations

import ast
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.structure import baseline_shards, imports, locality
from scripts.structure.patch_points import Point, extract_points
from scripts.structure.placement import (
    PATCH_TOPS,
    ModuleIndex,
    Placement,
    place,
    unit_of,
)

SECTION = "patch_targets"
Sites = dict[str, list[int]]

_STDLIB = frozenset(sys.stdlib_module_names)
_KNOWN_THIRD_PARTY = frozenset(
    {
        "requests",
        "httpx",
        "psutil",
        "yaml",
        "redis",
        "psycopg",
        "urllib",
        "opentelemetry",
        "langgraph",
        "langchain_core",
        "pytest",
        "aiohttp",
        "pandas",
        "numpy",
    }
)

# The process-global environment: module -> (tier, why, whole_subtree). `whole_subtree` False
# means only names defined directly in that module or package door. The explicit list is the
# whole of class E; a module is here because it is read implicitly across many packages.
E_MODULES: dict[str, tuple[str, str, bool]] = {
    "base.config": (
        "config",
        "settings singleton and config views, read implicitly everywhere",
        True,
    ),
    "base.paths": ("paths", "ava_home() / repo_root() / directory accessors", False),
    "base.cluster.machine": (
        "identity",
        "machine name, role and gateway address of this host",
        False,
    ),
    "base.cluster": ("identity", "cluster record and home identity at the package door", False),
    "ava.sdk_surface.agent_identity": (
        "identity",
        "which agent am I (process-global agent id and actor)",
        False,
    ),
    "base.host.env.runtime_config": ("env-resolution", "resolves ava_home and env aliases", False),
    "base.host.env.dotenv_boot": (
        "env-resolution",
        ".env anchoring and boot-time env resolution",
        False,
    ),
    "base.host.env.bootstrap": (
        "env-resolution",
        "bootstrap config source (gateway fetch or local)",
        False,
    ),
    "base.log": ("ambient-service", "process-global loguru sinks", False),
    "base.db": (
        "ambient-service",
        "process-global DB connect and pool seam at the package door",
        False,
    ),
    "base.telemetry": ("ambient-service", "process-global event sink at the package door", False),
}

# Sub-kind of a boundary target, by the top-level module it names.
_BOUNDARY_KIND = {
    "time": "clock",
    "datetime": "clock",
    "random": "random",
    "uuid": "random",
    "secrets": "random",
    "subprocess": "process",
    "psutil": "process",
    "signal": "process",
    "multiprocessing": "process",
    "pty": "process",
    "asyncio": "concurrency",
    "threading": "concurrency",
    "concurrent": "concurrency",
    "queue": "concurrency",
    "httpx": "network",
    "requests": "network",
    "urllib": "network",
    "socket": "network",
    "aiohttp": "network",
    "http": "network",
    "ssl": "network",
    "websockets": "network",
    "os": "os",
    "platform": "os",
    "sys": "sys",
    "pathlib": "fs",
    "shutil": "fs",
    "tempfile": "fs",
    "io": "fs",
    "glob": "fs",
    "fcntl": "fs",
    "stat": "fs",
    "zipfile": "fs",
    "psycopg": "db-driver",
    "psycopg_pool": "db-driver",
    "redis": "db-driver",
    "tests": "test-harness",
}


def boundary_kind(top: str) -> str:
    """Sub-kind of a class A target named by its top-level module."""
    return _BOUNDARY_KIND.get(top, "stdlib-other" if top in _STDLIB else "third-party")


def e_lookup(module: str) -> tuple[str, str] | None:
    """Longest-prefix match in `E_MODULES` -> (module prefix, tier)."""
    best: tuple[str, str] | None = None
    for prefix, (tier, _why, subtree) in E_MODULES.items():
        matches = module == prefix or (subtree and module.startswith(prefix + "."))
        if matches and (best is None or len(prefix) > len(best[0])):
            best = (prefix, tier)
    return best


@dataclass(frozen=True)
class Site:
    """One classified patch point."""

    line: int
    cat: str  # A B C D E U
    target: str  # the patched target as written or resolved
    sub: str = ""  # A: boundary kind; E: tier
    module: str = ""  # first-party module (B, C, D) or E module prefix
    key: str = ""  # D: the baseline target (Rule 4 style, cut at the first private part)
    owner: str = ""  # first-party: the owning package directory
    relation: str = ""  # D: ancestor | other-unit | sibling | top-level
    deep: bool = False  # C: a public attribute of an attribute of the module
    env_setter: bool = False


def _relation(home: str | None, owner: str) -> str:
    if home is None:
        return "top-level"
    if owner.startswith(home + "/"):
        return "ancestor"
    home_unit, owner_unit = unit_of(home.replace("/", ".")), unit_of(owner.replace("/", "."))
    return "sibling" if home_unit == owner_unit else "other-unit"


class Classifier:
    """Classifies patch points; caches per-module import maps for one run."""

    def __init__(self, index: ModuleIndex) -> None:
        self.index = index
        self._origins: dict[str, dict[str, str]] = {}

    def _import_origins(self, module: str) -> dict[str, str]:
        """local name -> dotted origin for every import in the module's own source."""
        if module not in self._origins:
            base = self.index.repo_root.joinpath(*module.split("."))
            path = base.with_suffix(".py")
            path = path if path.is_file() else base / "__init__.py"
            self._origins[module] = _origins_of(
                path, path.relative_to(self.index.repo_root).as_posix()
            )
        return self._origins[module]

    def classify(self, point: Point, home: str | None) -> Site:
        line = point.line
        if point.form == "ambient":
            return Site(line, "A", point.expr, sub="os")
        if point.form == "env":
            key = point.env or ""
            if key.startswith("AVA_"):
                return Site(
                    line, "E", f"env:{key}", sub="env-var", module="env:AVA_*", env_setter=True
                )
            return Site(line, "A", f"env:{key or '?'}", sub="os-env", env_setter=True)
        if point.dotted is None:
            return self._unresolved(point)
        split = self.index.split(point.dotted)
        if split is None:
            return Site(line, "A", point.dotted, sub=boundary_kind(point.dotted.split(".")[0]))
        return self._first_party(line, point.dotted, split, home)

    @staticmethod
    def _unresolved(point: Point) -> Site:
        # `script_module.subprocess.run`-style chains still name an external module
        target = point.expr + "." + (point.attr or "?")
        for segment in point.expr.split(".")[1:]:
            if segment in _STDLIB or segment in _KNOWN_THIRD_PARTY:
                return Site(point.line, "A", target, sub=boundary_kind(segment))
        return Site(point.line, "U", target)

    def _first_party(
        self, line: int, dotted: str, split: tuple[str, list[str]], home: str | None
    ) -> Site:
        module, remainder = split
        namespace = self._namespace_binding(line, dotted, module, remainder)
        if namespace is not None:
            return namespace
        ambient = e_lookup(module)
        if ambient:
            return Site(line, "E", dotted, sub=ambient[1], module=ambient[0])
        private = self._private(dotted, module)
        owner = private[1] if private else self.index.dir_of(module)
        own = home is not None and (home == owner or home.startswith(owner + "/"))
        if own:
            return Site(line, "B", dotted, module=module, owner=owner)
        if private:
            relation = _relation(home, owner)
            return Site(
                line, "D", dotted, module=module, key=private[0], owner=owner, relation=relation
            )
        return Site(line, "C", dotted, module=module, owner=owner, deep=len(remainder) >= 2)

    def _private(self, dotted: str, module: str) -> tuple[str, str] | None:
        """(baseline target, owning package directory) of the first private component.

        Rule 4's owner (`locality._private_target`) for a private module or package segment.
        A private attribute past the module boundary (`mod.Class._name`, `mod._name`) belongs
        to the package of the module that defines it.
        """
        found = locality._private_target(dotted, PATCH_TOPS, self.index.repo_root)
        if found is None:
            return None
        target, owner = found
        if target.count(".") >= module.count(".") + 1:  # the private part is an attribute
            return target, self.index.dir_of(module)
        return target, owner.replace(".", "/")

    def _namespace_binding(
        self, line: int, dotted: str, module: str, remainder: list[str]
    ) -> Site | None:
        """The patched name may just be the module's own binding of an external or ambient import
        (`module.time`, `module.settings`): judge it by where it really comes from."""
        origin = self._import_origins(module).get(remainder[0]) if remainder else None
        if not origin:
            return None
        top = origin.split(".")[0]
        if top not in PATCH_TOPS:
            return Site(line, "A", dotted, sub=boundary_kind(top))
        origin_split = self.index.split(origin)
        ambient = e_lookup(origin_split[0]) if origin_split else None
        if ambient:
            return Site(line, "E", dotted, sub=ambient[1], module=ambient[0])
        return None


def _origins_of(path: Path, rel_path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            out.update(imports.normalize(node, rel_path).origins)
    return out


@dataclass(frozen=True)
class FileResult:
    """One test file: where it lives (its home) and every classified patch point."""

    home: str | None
    fallback: bool
    sites: tuple[Site, ...]


def analyze(rel_path: str, text: str, classifier: Classifier) -> FileResult:
    """Classify the patch points of one test file (`rel_path` is repo-relative, POSIX)."""
    tree = ast.parse(text, filename=rel_path)
    nodes = list(ast.walk(tree))
    points = extract_points(nodes, rel_path)
    if not points:
        return FileResult(None, fallback=False, sites=())
    placement: Placement = place(rel_path, tree, classifier.index, nodes)
    support_home = _support_home(rel_path)
    if support_home is not None:
        placement = Placement(support_home, placement.unit)
    sites = tuple(classifier.classify(point, placement.home) for point in points)
    return FileResult(placement.home, placement.fallback, sites)


def _support_home(rel_path: str) -> str | None:
    """The package a support module (not `test_*`) of a `<pkg>/tests/` directory serves.

    A helper has no subject of its own, so its references say nothing about where it
    belongs; it is filed in the package whose tests directory holds it.
    """
    parts = rel_path.split("/")
    if parts[-1].startswith("test_") or "tests" not in parts[1:]:
        return None
    return "/".join(parts[: parts.index("tests", 1)])


def violations(rel_path: str, result: FileResult) -> Sites:
    """`path::target -> [line, ...]` for the class D sites of one file."""
    found: Sites = {}
    for site in result.sites:
        if site.cat == "D":
            found.setdefault(f"{rel_path}::{site.key}", []).append(site.line)
    return found


def _message(site: Site, home: str | None) -> str:
    private, owner = f"`{site.key}`", f"`{owner_dotted(site.owner)}`"
    if site.relation == "ancestor":
        return (
            f"this test lives in `{home}` but patches the private name {private} of the descendant "
            f"package {owner}: move the test down into {owner} (a test that also needs a package "
            f"{owner} does not import must be split), or give {owner} a public entry point / "
            "injection seam (a parameter, a settings field, a public setter) and patch that"
        )
    where = "a top-level test with no package home" if home is None else f"a test in `{home}`"
    return (
        f"{where} patches the private name {private} of package {owner}, which it does not belong "
        f"to: give {owner} a public entry point or accept the dependency as a parameter, and patch "
        "the public name"
    )


def owner_dotted(owner_dir: str) -> str:
    return owner_dir.replace("/", ".")


def new_site_errors(rel_path: str, result: FileResult, frozen: dict[str, int]) -> list[str]:
    """Messages for the class D sites of `result` beyond their frozen counts."""
    errors: list[str] = []
    by_key: dict[str, list[Site]] = collections.defaultdict(list)
    for site in result.sites:
        if site.cat == "D":
            by_key[f"{rel_path}::{site.key}"].append(site)
    for key, sites in sorted(by_key.items()):
        if len(sites) <= frozen.get(key, 0):
            continue
        suffix = f" (grew above its frozen count {frozen[key]})" if key in frozen else ""
        errors.extend(
            f"{rel_path}:{site.line}: {_message(site, result.home)}{suffix}"
            for site in sorted(sites, key=lambda s: s.line)
        )
    return errors


def stale_errors(
    measured: Sites, frozen: dict[str, int], scanned: set[str], repo_root: Path
) -> list[str]:
    """Frozen entries of scanned (or deleted) files whose sites shrank below the count."""
    return locality._stale_errors(SECTION, measured, frozen, scanned, repo_root)


def read_baseline(repo_root: Path) -> dict[str, int]:
    """The frozen `patch_targets` entries of every baseline shard (validated like `merge`)."""
    texts: dict[str, str] = {}
    for name, text in baseline_shards.read_worktree(repo_root).items():
        shard = cast("object", json.loads(text))
        if isinstance(shard, dict) and (section := cast("dict[str, object]", shard).get(SECTION)):
            texts[name] = json.dumps({SECTION: section})
    merged = baseline_shards.merge(texts, [SECTION])[SECTION]
    locality.validate_entries(SECTION, merged, PATCH_TOPS)
    return merged
