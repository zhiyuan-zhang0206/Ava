"""Lexical import bindings: what a name refers to at a use site."""

from __future__ import annotations

import ast
from collections.abc import Iterable

__all__ = ["Scope", "absolute_base", "collect_scope", "dotted_chain", "parameter_names"]


class Scope:
    """Import origins and shadowing names of one function, class or module body."""

    __slots__ = ("imports", "is_class", "parent", "shadowed", "stores", "values")

    def __init__(self, parent: Scope | None, *, is_class: bool = False) -> None:
        self.parent = parent
        self.is_class = is_class
        self.imports: dict[str, str] = {}
        self.shadowed: set[str] = set()
        self.stores: dict[str, int] = {}
        self.values: dict[str, ast.expr] = {}

    def constant(self, name: str) -> ast.expr | None:
        """The value of a name assigned exactly once in the nearest scope that binds it."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.shadowed:
                return scope.values.get(name) if scope.stores.get(name) == 1 else None
            scope = scope.parent
        return None

    def bind_import(self, name: str, origin: str) -> None:
        known = self.imports.get(name)
        if known is not None and known != origin:
            self.shadowed.add(name)
        self.imports[name] = origin

    def find(self, name: str) -> tuple[str | None, bool]:
        """(import origin, whether any scope binds the name)."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.shadowed:
                return None, True
            origin = scope.imports.get(name)
            if origin is not None:
                return origin, True
            scope = scope.parent
        return None, False


def absolute_base(node: ast.ImportFrom, package: list[str]) -> str | None:
    """Dotted base of a `from` import, or None when a relative import leaves the root."""
    if node.level == 0:
        return node.module or ""
    drop = node.level - 1
    if drop > len(package):
        return None
    anchor = package[: len(package) - drop]
    return ".".join([*anchor, *([node.module] if node.module else [])])


def _bind_import(scope: Scope, node: ast.Import | ast.ImportFrom, package: list[str]) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                scope.bind_import(alias.asname, alias.name)
            else:
                head = alias.name.split(".", 1)[0]
                scope.bind_import(head, head)
        return
    base = absolute_base(node, package)
    if base is None:
        return
    for alias in node.names:
        if alias.name != "*":
            origin = f"{base}.{alias.name}" if base else alias.name
            scope.bind_import(alias.asname or alias.name, origin)


def _children(node: ast.AST) -> Iterable[ast.AST]:
    if isinstance(node, ast.comprehension):
        return [node.iter, *node.ifs]
    return ast.iter_child_nodes(node)


def _store(scope: Scope, target: ast.expr) -> None:
    """Record the names an assignment target binds."""
    if isinstance(target, ast.Name):
        scope.shadowed.add(target.id)
        scope.stores[target.id] = scope.stores.get(target.id, 0) + 1
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            _store(scope, element)
    elif isinstance(target, ast.Starred):
        _store(scope, target.value)


def _assigned_value(scope: Scope, node: ast.Assign | ast.AnnAssign) -> None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) == 1 and isinstance(targets[0], ast.Name) and node.value is not None:
        scope.values[targets[0].id] = node.value


def _assigned(scope: Scope, node: ast.Assign | ast.AnnAssign) -> None:
    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
        _store(scope, target)
    _assigned_value(scope, node)


_NESTED_BODIES = (
    (ast.If, ("body", "orelse")),
    (ast.While, ("body", "orelse")),
    (ast.For, ("body", "orelse")),
    (ast.AsyncFor, ("body", "orelse")),
    (ast.With, ("body",)),
    (ast.AsyncWith, ("body",)),
    (ast.Try, ("body", "orelse", "finalbody")),
    (ast.TryStar, ("body", "orelse", "finalbody")),
)


def collect_scope(
    scope: Scope, body: Iterable[ast.AST], package: list[str], *, walrus: bool = False
) -> None:
    """Record the bindings made directly in one body, not in nested scopes.

    Bindings occur at statement level, so only statements and their targets are
    visited; `walrus` falls back to a full walk because `:=` can bind anywhere.
    """
    if walrus:
        _collect_everything(scope, body, package)
        return
    stack = list(body)
    while stack:
        node = stack.pop()
        kind = type(node)
        if kind is ast.Import or kind is ast.ImportFrom:
            _bind_import(scope, node, package)  # type: ignore[arg-type]
        elif kind in {ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef}:
            scope.shadowed.add(node.name)  # type: ignore[attr-defined]
        elif kind is ast.Assign or kind is ast.AnnAssign:
            _assigned(scope, node)  # type: ignore[arg-type]
        elif kind is ast.AugAssign:
            _store(scope, node.target)  # type: ignore[attr-defined]
        else:
            _collect_compound(scope, node, stack, package)


def _collect_compound(
    scope: Scope, node: ast.AST, stack: list[ast.AST], package: list[str]
) -> None:
    for kinds, fields in _NESTED_BODIES:
        if isinstance(node, kinds):
            for field in fields:
                stack.extend(getattr(node, field))
            break
    else:
        if isinstance(node, ast.Match):
            _collect_everything(scope, [node], package)
            return
    if isinstance(node, ast.For | ast.AsyncFor):
        _store(scope, node.target)
    elif isinstance(node, ast.With | ast.AsyncWith):
        for item in node.items:
            if item.optional_vars is not None:
                _store(scope, item.optional_vars)
    elif isinstance(node, ast.Try | ast.TryStar):
        for handler in node.handlers:
            if handler.name:
                scope.shadowed.add(handler.name)
            stack.extend(handler.body)


def _collect_everything(scope: Scope, body: Iterable[ast.AST], package: list[str]) -> None:
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope.shadowed.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            _bind_import(scope, node, package)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            _store(scope, node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            _assigned_value(scope, node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            scope.shadowed.add(node.name)
        stack.extend(_children(node))


def parameter_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    names.extend(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    return names


def dotted_chain(node: ast.expr) -> tuple[ast.Name, list[str]] | None:
    """`a.b.c` as (Name a, ["b", "c"]); None unless the chain is plain names."""
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    attrs.reverse()
    return node, attrs
