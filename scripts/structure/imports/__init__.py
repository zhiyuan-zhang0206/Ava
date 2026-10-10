"""Static import facts shared by structure, dependency and test-placement checks.

The checkout path supplies Python's package anchor, including namespace test
directories loaded by pytest's importlib mode. A clause binds names and names
direct dependencies; it does not execute imports or follow re-exported values.
Dynamic imports and patch expressions remain evidence owned by their collectors.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "Binding",
    "Clause",
    "Dependency",
    "DependencyEvidence",
    "IncompleteImportError",
    "InvalidRelativeImportError",
    "ModuleLookup",
    "ModuleSourceLookup",
    "dependencies",
    "dependency_evidence",
    "import_base",
    "normalize",
    "package_of",
]


class ModuleLookup(Protocol):
    """The current checkout's module resolver, independent of import syntax."""

    def kind(self, dotted: str) -> str | None: ...

    def resolve_prefix(self, dotted: str, tops: Sequence[str]) -> str | None: ...


class ModuleSourceLookup(ModuleLookup, Protocol):
    """Resolve exact source files and anchored resources in the same checkout."""

    repo_root: Path

    def file(self, dotted: str) -> str | None: ...


class InvalidRelativeImportError(ImportError):
    """An explicit relative import has no legal package anchor."""


def package_of(rel_path: str) -> list[str]:
    """Package parts of a module file; an initializer owns its containing package."""
    return rel_path.removesuffix(".py").split("/")[:-1]


def import_base(node: ast.ImportFrom, rel_path: str) -> str | None:
    """Absolute import base, or None when the relative import escapes its package."""
    if node.level == 0:
        return node.module or ""
    package = package_of(rel_path)
    if node.level > len(package):
        return None
    anchor = package[: len(package) - (node.level - 1)]
    return ".".join([*anchor, *([node.module] if node.module else [])])


@dataclass(frozen=True)
class Binding:
    """A local name, the imported target and the dotted origin that name binds."""

    name: str
    target: str
    origin: str


@dataclass(frozen=True)
class Clause:
    """One normalized import statement, retaining aliases and its source line."""

    line: int
    base: str | None
    bindings: tuple[Binding, ...]
    statement: str

    @property
    def candidates(self) -> list[str]:
        """Raw targets, including members, for private-name reach-in checks."""
        return ([self.base] if self.base else []) + [b.target for b in self.bindings]

    @property
    def origins(self) -> dict[str, str]:
        """Names bound by the statement; star imports cannot bind a known name."""
        return {b.name: b.origin for b in self.bindings if b.name != "*"}

    def module_aliases(self, is_module: Callable[[str], bool]) -> dict[str, str]:
        """Only bindings that name modules, rather than functions or classes."""
        return {
            b.name: b.origin
            for b in self.bindings
            if b.name != "*" and (self.base is None or is_module(b.target))
        }


@dataclass(frozen=True)
class Dependency:
    """One direct module dependency in a clause, with all its bound local names."""

    module: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class DependencyEvidence:
    """Exact first-party edges and targets whose import cannot be established."""

    resolved: tuple[Dependency, ...]
    unknown: tuple[str, ...]


class IncompleteImportError(ImportError):
    """The refs-only API cannot represent an unresolved first-party import."""


def normalize(node: ast.Import | ast.ImportFrom, rel_path: str) -> Clause:
    """Normalize syntax without importing modules or interpreting their values."""
    if isinstance(node, ast.Import):
        bindings = tuple(
            Binding(
                alias.asname or alias.name.split(".")[0],
                alias.name,
                alias.name if alias.asname else alias.name.split(".")[0],
            )
            for alias in node.names
        )
        return Clause(node.lineno, None, bindings, ast.unparse(node))
    base = import_base(node, rel_path)
    if base is None:
        raise InvalidRelativeImportError(
            f"{rel_path}:{node.lineno}: relative import has no legal parent package: "
            f"{ast.unparse(node)}"
        )
    bindings = tuple(
        Binding(alias.asname or alias.name, target, target)
        for alias in node.names
        for target in [base if alias.name == "*" else f"{base}.{alias.name}"]
    )
    statement = ast.unparse(ast.ImportFrom(module=base, names=node.names, level=0))
    return Clause(node.lineno, base, bindings, statement)


def dependency_evidence(
    clause: Clause, index: ModuleLookup, tops: Sequence[str]
) -> DependencyEvidence:
    """Resolve direct module edges once per clause against the current checkout.

    `from pkg import module` names the submodule when it exists; other imported
    members depend on pkg itself, even when pkg re-exports another module's value.
    Repeated members of that same module produce one edge, retaining all aliases.
    """
    found: dict[str, list[str]] = {}
    unknown: list[str] = []
    for binding in clause.bindings:
        target = binding.target
        if clause.base is not None and not index.kind(target):
            target = clause.base
        if target.split(".")[0] not in tops:
            continue
        if index.kind(target) is None:
            unknown.append(target)
        else:
            names = found.setdefault(target, [])
            if binding.name not in names:
                names.append(binding.name)
    return DependencyEvidence(
        tuple(Dependency(module, tuple(names)) for module, names in found.items()),
        tuple(dict.fromkeys(unknown)),
    )


def dependencies(clause: Clause, index: ModuleLookup, tops: Sequence[str]) -> list[Dependency]:
    """Exact direct edges; missing modules cannot silently become a parent package."""
    evidence = dependency_evidence(clause, index, tops)
    if evidence.unknown:
        raise IncompleteImportError(
            f"line {clause.line}: unresolved first-party import: {', '.join(evidence.unknown)}"
        )
    return list(evidence.resolved)
