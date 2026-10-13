"""Import access: resolving names to first-party files and recording import and attribute facts."""

from __future__ import annotations

import ast

from meizel.graph.model import Code, Fact, FactKind, Problem
from meizel.graph.scopes import Scope, absolute_base, collect_scope, dotted_chain
from meizel.graph.universe import Universe

__all__ = ["ImportAccess"]


class ImportAccess:
    """The scan state of one file and the facts that import access alone produces."""

    def __init__(self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool) -> None:
        self.u = universe
        self.path = path
        self.tree = tree
        self.package = path.split("/")[:-1]
        self.walrus = walrus
        self.facts: list[Fact] = []
        self.problems: list[Problem] = []
        self.scope = Scope(None)
        collect_scope(self.scope, tree.body, self.package, walrus=walrus)
        self.imported = set(self.scope.imports)

    def add(self, dst: str, line: int, kind: FactKind, names: tuple[str, ...], detail: str) -> None:
        self.facts.append(Fact(self.path, dst, line, kind, names, detail))

    def problem(self, line: int, message: str, code: Code = Code.UNRESOLVED_IMPORT) -> None:
        self.problems.append(Problem(line, code, message))

    # --- name resolution -----------------------------------------------------------

    def dotted(self, expr: ast.expr) -> str | None:
        chain = dotted_chain(expr)
        if chain is None:
            return None
        base, attrs = chain
        origin, _ = self.scope.find(base.id)
        return None if origin is None else ".".join([origin, *attrs])

    def module_file(self, expr: ast.expr) -> str | None:
        dotted = self.dotted(expr)
        if dotted is None or not self.u.is_first_party_name(dotted):
            return None
        module = self.u.resolve_module(dotted)
        return None if module is None else module.file

    # --- imports -------------------------------------------------------------------

    def visit_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not self.u.is_first_party_name(alias.name):
                continue
            module = self.u.resolve_module(alias.name)
            if module is None:
                self.problem(node.lineno, f"`import {alias.name}` names no tracked module")
            elif module.file is not None:
                self.add(module.file, node.lineno, FactKind.IMPORT, (), f"import {alias.name}")

    def visit_import_from(self, node: ast.ImportFrom) -> None:
        base = absolute_base(node, self.package)
        if base is None:
            self.problem(node.lineno, f"relative import leaves the repository: {ast.unparse(node)}")
            return
        if not node.level and not self.u.is_first_party_name(base):
            return
        for alias in node.names:
            self.from_name(node, base, alias.name)

    def from_name(self, node: ast.ImportFrom, base: str, name: str) -> None:
        line = node.lineno
        detail = f"from {'.' * node.level}{node.module or ''} import ..."
        if name != "*":
            candidate = f"{base}.{name}" if base else name
            submodule = self.u.resolve_module(candidate)
            if submodule is not None:
                if submodule.file is not None:
                    self.add(submodule.file, line, FactKind.IMPORT, (), detail)
                return
        owner = self.u.resolve_module(base) if base else None
        if owner is None or owner.file is None:
            self.problem(
                line,
                f"`{detail.removesuffix('...')}{name}` names no tracked module or package door",
            )
        else:
            self.add(owner.file, line, FactKind.IMPORT, (name,), detail)

    def module_uses(self, origin: str, attrs: list[str], line: int) -> None:
        if not self.u.is_first_party_name(origin):
            return
        module = self.u.resolve_module(origin)
        current = origin
        for attr in attrs:
            if module is None:
                return
            deeper = self.u.resolve_module(f"{current}.{attr}")
            if deeper is None:
                if module.file is not None:
                    self.add(module.file, line, FactKind.ATTRIBUTE, (attr,), f"{current}.{attr}")
                return
            current, module = f"{current}.{attr}", deeper
            if module.file is not None:
                self.add(module.file, line, FactKind.ATTRIBUTE, (), current)
