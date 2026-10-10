"""Lexical import origins and unambiguous bindings shared by dependency consumers."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from itertools import product
from typing import cast

from . import Binding, Clause, normalize

type ScopeNode = (
    ast.FunctionDef
    | ast.AsyncFunctionDef
    | ast.ClassDef
    | ast.Lambda
    | ast.ListComp
    | ast.SetComp
    | ast.DictComp
    | ast.GeneratorExp
)

_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def scope_parts(
    node: ScopeNode,
) -> tuple[tuple[ast.AST, ...], tuple[ast.AST, ...]]:
    """Definition-time expressions use the enclosing scope; only bodies use the new one."""
    if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
        outer = (node.generators[0].iter,)
        inner: list[ast.AST] = []
        for offset, generator in enumerate(node.generators):
            if offset:
                inner.append(generator.iter)
            inner.extend((generator.target, *generator.ifs))
        inner.extend((node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,))
        return outer, tuple(inner)
    if isinstance(node, ast.ClassDef):
        outer = (*node.decorator_list, *node.bases, *node.keywords, *node.type_params)
        return outer, tuple(node.body)
    if isinstance(node, ast.Lambda):
        return (node.args,), (node.body,)
    outer = (node.args, *node.decorator_list, *node.type_params)
    if node.returns is not None:
        outer = (*outer, node.returns)
    return outer, tuple(node.body)


def local_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Walk this lexical scope, keeping nested scope bodies out of its bindings."""
    children = ast.iter_child_nodes(tree)
    if isinstance(tree, _SCOPE_NODES):
        children = iter(scope_parts(tree)[1])
    pending = [(children, False)]
    while pending:
        children, scope_root = pending[-1]
        child = next(children, None)
        if child is None:
            pending.pop()
            continue
        yield child
        if isinstance(child, _SCOPE_NODES):
            outer, inner = scope_parts(child)
            # Definition-time expressions are separate roots, including lambda decorators.
            pending.append((iter(inner if scope_root else outer), not scope_root))
        elif child._fields:
            pending.append((ast.iter_child_nodes(child), False))


class Scope:
    """One lexical scope, without evaluating Python or guessing rebound values."""

    def __init__(self, tree: ast.AST, path: str, parent: Scope | None = None) -> None:
        self.parent = parent
        self.is_class = isinstance(tree, ast.ClassDef)
        self.values: dict[str, ast.expr] = {}
        self.origins: dict[str, str] = {}
        self.import_counts: Counter[str] = Counter()
        self.import_clauses: dict[str, Clause] = {}
        self.attribute_writes: list[ast.Attribute] = []
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.stores: Counter[str] = Counter()
        self.domains: dict[str, tuple[str, ...]] = {}
        for node in local_nodes(tree):
            self._bind(node, path)
        if isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = tree.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self.stores[arg.arg] += 1
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    self.stores[arg.arg] += 1
            if isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef) and parent is not None:
                self.domains = parameter_domains(tree, parent)

    def _bind(self, node: ast.AST, path: str) -> None:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self.stores[node.id] += 1
        elif isinstance(node, ast.Assign):
            self._assignment(node.targets, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            self.values[node.target.id] = node.value
        elif isinstance(node, ast.Import | ast.ImportFrom):
            self._import(node, path)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store | ast.Del):
            self.attribute_writes.append(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            self.functions[node.name] = node
            self.stores[node.name] += 1
        elif isinstance(node, ast.ClassDef) or (
            isinstance(node, ast.ExceptHandler) and node.name is not None
        ):
            self.stores[cast(str, node.name)] += 1

    def _assignment(self, targets: list[ast.expr], value: ast.expr) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                self.values[target.id] = value

    def _import(self, node: ast.Import | ast.ImportFrom, path: str) -> None:
        if isinstance(node, ast.ImportFrom) and node.level and not path:
            return
        clause = normalize(node, path)
        for name, origin in clause.origins.items():
            previous = self.origins.get(name, origin)
            self.origins[name] = origin if previous == origin else ""
            self.import_counts[name] += 1
            self.import_clauses[name] = clause
            self.stores[name] += 1

    def import_binding(
        self, node: ast.expr, seen: frozenset[str] = frozenset()
    ) -> tuple[Clause, Binding] | None:
        """An expression's one import binding, retaining module versus member syntax."""
        if isinstance(node, ast.Attribute):
            return self.import_binding(node.value, seen)
        if not isinstance(node, ast.Name) or node.id in seen:
            return None
        if node.id not in self.stores and self.parent is not None:
            return self.parent.import_binding(node, seen)
        if self.stores[node.id] != 1:
            return None
        if node.id in self.values:
            return self.import_binding(self.values[node.id], seen | {node.id})
        clause = self.import_clauses.get(node.id)
        if clause is None:
            return None
        binding = next(binding for binding in clause.bindings if binding.name == node.id)
        return clause, binding

    def unmodified_origin(self, node: ast.expr) -> str:
        """Imported origin unless this lexical chain writes an attribute on its path."""
        origin = self.origin(node)
        scope: Scope | None = self
        while origin and scope is not None:
            for written in scope.attribute_writes:
                target = scope.origin(written)
                if target and (origin == target or origin.startswith(target + ".")):
                    return ""
            scope = scope.parent
        return origin

    def value(self, node: ast.expr) -> ast.expr:
        """One unambiguous plain binding; parameters and rebinding remain opaque."""
        if not isinstance(node, ast.Name):
            return node
        if node.id not in self.stores and self.parent is not None:
            return self.parent.value(node)
        return self.values.get(node.id, node) if self.stores[node.id] == 1 else node

    def origin(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> str:
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value, seen)
            return f"{base}.{node.attr}" if base else ""
        if not isinstance(node, ast.Name):
            return ""
        if node.id in seen:
            return ""
        if node.id not in self.stores and self.parent is not None:
            return self.parent.origin(node, seen)
        if node.id == "__import__" and node.id not in self.stores:
            return node.id
        if self.stores[node.id] == 1 and node.id in self.values:
            return self.origin(self.values[node.id], seen | {node.id})
        return (
            self.origins.get(node.id, "")
            if self.stores[node.id] == self.import_counts[node.id]
            else ""
        )

    def function(self, name: str) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, Scope] | None:
        if name not in self.stores and self.parent is not None:
            return self.parent.function(name)
        node = self.functions.get(name)
        return (node, self) if node is not None and self.stores[name] == 1 else None

    def nested_parent(self) -> Scope:
        scope = self
        while scope.is_class and scope.parent is not None:
            scope = scope.parent
        return scope

    def bound(self, name: str) -> bool:
        """Whether source binds a name in this scope or an enclosing lexical scope."""
        return name in self.stores or (self.parent is not None and self.parent.bound(name))

    def strings(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> tuple[str, ...] | None:
        """Literal texts, one binding, or a bounded literal pytest parameter domain."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value,)
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            if node.id not in self.stores and self.parent is not None:
                return self.parent.strings(node, seen)
            if self.stores[node.id] != 1:
                return None
            if node.id in self.domains:
                return self.domains[node.id]
            value = self.values.get(node.id)
            return self.strings(value, seen | {node.id}) if value is not None else None
        if isinstance(node, ast.JoinedStr):
            return self._fstring(node, seen)
        return None

    def _fstring(self, node: ast.JoinedStr, seen: frozenset[str]) -> tuple[str, ...] | None:
        chunks: list[tuple[str, ...]] = []
        combinations = 1
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return None
                values = self.strings(part.value, seen)
            else:
                values = self.strings(part, seen)
            if values is None:
                return None
            chunks.append(values)
            combinations *= len(values)
        if combinations > 256:
            return None
        return tuple(dict.fromkeys("".join(parts) for parts in product(*chunks)))


def _parameter_domain(decorator: ast.expr, parent: Scope) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(decorator, ast.Call) or len(decorator.args) < 2:
        return None
    if parent.origin(decorator.func) != "pytest.mark.parametrize":
        return None
    if any(kw.arg in {"indirect", "argnames", "argvalues"} for kw in decorator.keywords):
        return None
    name, values = decorator.args[:2]
    if (
        not isinstance(name, ast.Constant)
        or not isinstance(name.value, str)
        or not name.value.isidentifier()
    ):
        return None
    texts = _literal_texts(values)
    return (name.value, texts) if texts else None


def _literal_texts(values: ast.expr) -> tuple[str, ...] | None:
    if not isinstance(values, ast.List | ast.Tuple):
        return None
    if not all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values.elts):
        return None
    return tuple(
        v.value for v in values.elts if isinstance(v, ast.Constant) and isinstance(v.value, str)
    )


def parameter_domains(
    node: ast.FunctionDef | ast.AsyncFunctionDef, parent: Scope
) -> dict[str, tuple[str, ...]]:
    """Only plain literal string lists passed to the actual pytest decorator."""
    found: dict[str, tuple[str, ...]] = {}
    duplicate: set[str] = set()
    for decorator in node.decorator_list:
        domain = _parameter_domain(decorator, parent)
        if domain is None:
            continue
        name, values = domain
        if name in found:
            duplicate.add(name)
        found[name] = values
    return {name: values for name, values in found.items() if name not in duplicate}
