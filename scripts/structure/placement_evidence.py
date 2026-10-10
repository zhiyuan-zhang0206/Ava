"""First-party reference facts and syntax-derived repository-root path evidence."""

from __future__ import annotations

import ast
import collections
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from scripts.structure.imports import executed


def _last_name(node: ast.expr) -> str:
    """`Path` for `Path` and `pathlib.Path`, "" for anything that is not a plain name."""
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def _plus(base: int | None, extra: int) -> int | None:
    return None if base is None else base + extra


def _ascents_of_call(node: ast.Call, is_path: Callable[[ast.expr], bool] | None) -> int | None:
    name = _last_name(node.func)
    if (is_path(node.func) if is_path else name == "Path") and len(node.args) == 1:
        arg = node.args[0]
        return 0 if isinstance(arg, ast.Name) and arg.id == "__file__" else None
    if name in ("resolve", "absolute") and not node.args and isinstance(node.func, ast.Attribute):
        return file_ascents(node.func.value, is_path)
    return None


def _ascents_of_parents(
    node: ast.Subscript, is_path: Callable[[ast.expr], bool] | None
) -> int | None:
    parents, index = node.value, node.slice
    if (
        isinstance(parents, ast.Attribute)
        and parents.attr == "parents"
        and isinstance(index, ast.Constant)
        and isinstance(index.value, int)
    ):
        return _plus(file_ascents(parents.value, is_path), index.value + 1)
    return None


def file_ascents(node: ast.AST, is_path: Callable[[ast.expr], bool] | None = None) -> int | None:
    """How many directories above the file `Path(__file__)...` climbs (`.parents[2]`: 3), or None."""
    if isinstance(node, ast.Call):
        return _ascents_of_call(node, is_path)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _plus(file_ascents(node.value, is_path), 1)
    if isinstance(node, ast.Subscript):
        return _ascents_of_parents(node, is_path)
    return None


def _bindings(
    tree: ast.AST,
) -> tuple[dict[str, list[ast.expr]], collections.Counter[str], set[str]]:
    """(the value of every plain assignment by name, how often each name is stored, the names
    bound to something unseen: parameters and imports)."""
    bound: dict[str, list[ast.expr]] = collections.defaultdict(list)
    stores: collections.Counter[str] = collections.Counter()
    opaque: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id].append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            bound[node.target.id].append(node.value)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[node.id] += 1  # also loop, `with`, unpacking and augmented bindings
        elif isinstance(node, ast.arg):
            opaque.add(node.arg)
        elif isinstance(node, ast.alias):
            opaque.add((node.asname or node.name).split(".")[0])
    return bound, stores, opaque


class RepoRoots:
    """The expressions of one file that are the repository root."""

    def __init__(self, tree: ast.AST, rel_path: str) -> None:
        # the file sits `depth` directories below the root, so that many climbs from it reach it
        self._depth = len(PurePosixPath(rel_path).parts) if rel_path else 0
        self._names: set[str] = set()
        bound, stores, opaque = _bindings(tree)
        grew = True
        while grew:  # `ROOT = HERE` follows `HERE = Path(__file__)...`
            grew = False
            for name, values in bound.items():
                plain = stores[name] == len(values) and name not in opaque
                if plain and name not in self._names and all(self.is_root(v) for v in values):
                    self._names.add(name)
                    grew = True

    def is_root(self, node: ast.AST) -> bool:
        """`repo_root()`, a name bound (in any scope) only to the root, or a climb from
        `Path(__file__)` that ends exactly at it."""
        if isinstance(node, ast.Name):
            return node.id in self._names
        if isinstance(node, ast.Call) and _last_name(node.func) == "repo_root":
            return not node.args
        return self._depth > 0 and file_ascents(node) == self._depth


@dataclass
class Ref:
    """One first-party module a file references."""

    line: int
    kind: str  # import | string-target | string-loose | embedded-import | path-file | path-dir
    module: str
    unit: str
    via: str = ""  # callee of a string target
    names: tuple[str, ...] = ()  # local names an import binds (patch-evidence pruning)


@dataclass
class ReferenceEvidence:
    """Known references with execution inputs whose dependencies remain unresolved."""

    refs: list[Ref]
    unresolved: list[executed.Unresolved]


class IncompleteReferenceEvidenceError(ValueError):
    """The legacy list API cannot represent incomplete execution evidence."""

    def __init__(self, evidence: ReferenceEvidence) -> None:
        self.evidence = evidence
        details = "; ".join(f"{u.path}:{u.line}: {u.reason}" for u in evidence.unresolved)
        super().__init__(details)


@dataclass(frozen=True)
class Placement:
    """The package a test file belongs to: `home` is a code directory such as `base/host`."""

    home: str | None
    unit: str | None = None
    fallback: bool = False  # the patch-evidence fallback applied
    ambiguous: bool = False  # no unit could legally import all the others


@dataclass(frozen=True)
class LegacyPlacement:
    """Existing patch-gate inference paired with its complete or incomplete facts.

    This adapter is only for the existing private-patch consumer during cleanup.
    New locality checks must consume ReferenceEvidence directly; an inferred home
    from incomplete facts does not certify placement or grant new private access.
    """

    placement: Placement
    evidence: ReferenceEvidence


@dataclass(frozen=True)
class SubjectLCA:
    """A complete Python subject directory proof (empty string is root), or its gaps."""

    directory: str | None
    modules: tuple[str, ...]
    unknown: tuple[executed.Unresolved, ...]


def subject_lca(tree: ast.AST, rel: str, index: object) -> SubjectLCA:
    """Resolve a directory LCA without import direction or all-patch fallback.

    Shared facts are the only parser. Test support and resources cannot prove
    Python subjects; all unknown inputs still prevent certification.
    """
    from scripts.structure import placement
    from scripts.structure.imports import facts

    if not isinstance(index, placement.ModuleIndex):
        raise TypeError("subject LCA requires the shared module index")
    evidence = facts.collect(tree, rel, index, tops=placement.CODE_TOPS)
    refs: list[Ref] = []
    for fact in evidence.records:
        if fact.kind == facts.FactKind.RESOURCE or "tests" in fact.target.split("."):
            continue
        unit = placement.unit_of(fact.target)
        if unit is None:
            continue
        kind = "string-target" if fact.kind == facts.FactKind.DYNAMIC_IMPORT else "import"
        if fact.kind in {facts.FactKind.EMBEDDED_IMPORT, facts.FactKind.PYTHON_MODULE}:
            kind = "embedded-import"
        refs.append(Ref(fact.line, kind, fact.target, unit, fact.via, fact.names))
    pruned = placement.without_patch_evidence(list(ast.walk(tree)), refs)
    modules = tuple(sorted({ref.module for ref in pruned}))
    directory = (
        placement.common_dir([index.dir_of(module) for module in modules])
        if modules and not evidence.unknown
        else None
    )
    gaps = tuple(executed.Unresolved(u.path, u.line, u.reason) for u in evidence.unknown)
    return SubjectLCA(directory, modules, gaps)
