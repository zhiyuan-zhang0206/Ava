"""Proofs for mock's object form, independent of string import and placement policy."""

import ast
from pathlib import Path
from typing import Protocol

from . import ModuleLookup, bindings


class Lookup(ModuleLookup, Protocol):
    repo_root: Path

    def file(self, dotted: str) -> str | None: ...


def is_object(node: ast.expr, scope: bindings.Scope, index: Lookup) -> bool:
    """Prove a collection, standard mapping or imported module without evaluating it."""
    if isinstance(scope.value(node), ast.Dict | ast.List | ast.Tuple | ast.Set):
        return True
    origin = scope.unmodified_origin(node)
    module, _, member = origin.rpartition(".")
    if (module, member) in {("os", "environ"), ("sys", "modules")}:
        return True
    imported = scope.import_binding(node)
    if not origin or imported is None:
        return False
    clause, binding = imported
    if clause.base is None:
        # `import pkg.child` binds a module and loads every prefix of its target.
        return binding.target == origin or binding.target.startswith(origin + ".")
    if origin != binding.origin or index.kind(origin) is None:
        return False  # An attribute of an imported member may still be a string.
    return _inert_package(clause.base, index)


def _inert_package(module: str, index: Lookup) -> bool:
    """A namespace or docstring-only door cannot re-export a string over a submodule."""
    if index.kind(module) == "ns":
        return True
    path = index.file(module)
    if path is None or index.kind(module) != "pkg":
        return False
    tree = ast.parse((index.repo_root / path).read_text(encoding="utf-8"), filename=path)
    return all(
        isinstance(stmt, ast.Pass)
        or (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
        for stmt in tree.body
    )
