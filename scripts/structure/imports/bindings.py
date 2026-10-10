"""Lexical import origins and unambiguous bindings shared by dependency consumers."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from itertools import product
from pathlib import Path
from typing import cast

from . import Binding, Clause, normalize

__all__ = ["Scope", "ScopeNode", "local_nodes", "parameter_domains", "scope_parts"]

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
_BINDING_NODES = (
    ast.Name,
    ast.Assign,
    ast.AnnAssign,
    ast.Import,
    ast.ImportFrom,
    ast.Attribute,
    ast.For,
    ast.AsyncFor,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.ExceptHandler,
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


_MUTATORS = frozenset(
    {
        "__delitem__",
        "__setitem__",
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)
_LITERAL_LIMIT = 256


class ModuleContext:
    """Whole-module facts that let one reader resolve bounded literal tables.

    ``name`` is the runtime ``__name__`` of the analyzed source. ``mutated``
    names every binding whose container contents may change after it is bound;
    their subscripts and loop elements stay opaque. It is computed on first use,
    so modules without a literal-table lookup never pay the extra walk.
    """

    def __init__(self, tree: ast.AST, name: str) -> None:
        self.name = name
        self._tree = tree
        self._mutated: frozenset[str] | None = None
        self._scopes: dict[tuple[ast.AST, str, Scope | None], Scope] = {}

    def scope(self, tree: ast.AST, path: str, parent: Scope | None = None) -> Scope:
        """Reuse a completed lexical scope within this source analysis only."""
        key = (tree, path, parent)
        if key not in self._scopes:
            self._scopes[key] = Scope(tree, path, parent, context=self)
        return self._scopes[key]

    def clear_scopes(self) -> None:
        """Release query-owned scopes, including their references to this context."""
        self._scopes.clear()

    @property
    def mutated(self) -> frozenset[str]:
        if self._mutated is None:
            self._mutated = _mutated_names(self._tree)
        return self._mutated


def module_context(tree: ast.AST, name: str) -> ModuleContext:
    """The module-level context for one parsed source, without evaluating code."""
    return ModuleContext(tree, name)


def _plain_aliases(node: ast.AST) -> tuple[tuple[str, str], ...]:
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
        return tuple(
            (target.id, node.value.id) for target in node.targets if isinstance(target, ast.Name)
        )
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Name)
    ):
        return ((node.target.id, node.value.id),)
    return ()


def _direct_mutations(node: ast.AST) -> tuple[str, ...]:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Store | ast.Del)
        and isinstance(node.value, ast.Name)
    ):
        return (node.value.id,)
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return (node.target.id,)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATORS
        and isinstance(node.func.value, ast.Name)
    ):
        return (node.func.value.id,)
    return tuple(node.names) if isinstance(node, ast.Global | ast.Nonlocal) else ()


def _mutated_names(tree: ast.AST) -> frozenset[str]:
    """In-module container writes and rebinding, propagated through plain aliases."""
    mutated: set[str] = set()
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        for left, right in _plain_aliases(node):
            aliases.setdefault(left, set()).add(right)
            aliases.setdefault(right, set()).add(left)
        mutated.update(_direct_mutations(node))
        if isinstance(node, ast.Call):
            arguments = (*node.args, *(keyword.value for keyword in node.keywords))
            mutated.update(argument.id for argument in arguments if isinstance(argument, ast.Name))
    pending = list(mutated)
    while pending:
        for alias in aliases.get(pending.pop(), ()):
            if alias not in mutated:
                mutated.add(alias)
                pending.append(alias)
    return frozenset(mutated)


def module_name(path: str) -> str:
    """The import name of a repository-relative Python source path; empty for no path."""
    if not path:
        return ""
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


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

    def __init__(
        self,
        tree: ast.AST,
        path: str,
        parent: Scope | None = None,
        *,
        context: ModuleContext | None = None,
    ) -> None:
        self.parent = parent
        self.context = context if context is not None or parent is None else parent.context
        self.iterated: dict[str, ast.expr] = {}
        self.is_class = isinstance(tree, ast.ClassDef)
        self.values: dict[str, ast.expr] = {}
        self.origins: dict[str, str] = {}
        self.import_counts: Counter[str] = Counter()
        self.import_clauses: dict[str, Clause] = {}
        self.attribute_writes: list[ast.Attribute] = []
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.stores: Counter[str] = Counter()
        self.domains: dict[str, tuple[str, ...]] = {}
        self.calls: list[ast.Call] = []
        for node in local_nodes(tree):
            if isinstance(node, ast.Call):
                self.calls.append(node)
            elif isinstance(node, _BINDING_NODES):
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
        elif isinstance(node, ast.Attribute | ast.For | ast.AsyncFor):
            self._bind_statement(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            self.functions[node.name] = node
            self.stores[node.name] += 1
        elif isinstance(node, ast.ClassDef) or (
            isinstance(node, ast.ExceptHandler) and node.name is not None
        ):
            self.stores[cast(str, node.name)] += 1

    def _bind_statement(self, node: ast.Attribute | ast.For | ast.AsyncFor) -> None:
        if isinstance(node, ast.Attribute):
            if isinstance(node.ctx, ast.Store | ast.Del):
                self.attribute_writes.append(node)
        elif isinstance(node.target, ast.Name):
            self.iterated[node.target.id] = node.iter

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
        """Literal texts, one binding, or a bounded literal pytest parameter domain.

        With a module context, ``__name__``, subscripts of unmutated literal
        tables and loop variables over them also resolve to their finite values.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value,)
        if isinstance(node, ast.Name):
            return self._name_strings(node, seen)
        if isinstance(node, ast.JoinedStr):
            return self._fstring(node, seen)
        if (
            isinstance(node, ast.Subscript)
            and self.context is not None
            and not isinstance(node.slice, ast.Slice)
        ):
            return self._elements(node.value, seen, keys=False)
        return None

    def _name_strings(self, node: ast.Name, seen: frozenset[str]) -> tuple[str, ...] | None:
        if node.id == "__name__" and self.context is not None and not self.bound(node.id):
            return (self.context.name,)
        if node.id in seen:
            return None
        if node.id not in self.stores and self.parent is not None:
            return self.parent.strings(node, seen)
        if self.stores[node.id] != 1:
            return None
        if node.id in self.domains:
            return self.domains[node.id]
        value = self.values.get(node.id)
        if value is not None:
            return self.strings(value, seen | {node.id})
        iterated = self.iterated.get(node.id)
        if iterated is not None and self.context is not None:
            return self._elements(iterated, seen | {node.id}, keys=True)
        return None

    def _container(self, node: ast.expr, seen: frozenset[str]) -> tuple[ast.expr, Scope] | None:
        """The literal a container expression is bound to, unless it may be mutated."""
        if not isinstance(node, ast.Name):
            return node, self
        if node.id in seen or self.context is None or node.id in self.context.mutated:
            return None
        if node.id not in self.stores and self.parent is not None:
            return self.parent._container(node, seen)
        if self.stores[node.id] != 1 or node.id not in self.values:
            return None
        return self._container(self.values[node.id], seen | {node.id})

    def _elements(
        self, node: ast.expr, seen: frozenset[str], *, keys: bool
    ) -> tuple[str, ...] | None:
        """Every string a literal table yields: dict keys when iterated, values when indexed."""
        resolved = self._container(node, seen)
        if resolved is None:
            return None
        container, owner = resolved
        if isinstance(container, ast.Dict):
            if any(key is None for key in container.keys):
                return None  # ``**`` merges an opaque mapping.
            items: list[ast.expr] = (
                [key for key in container.keys if key is not None]
                if keys
                else list(container.values)
            )
        elif isinstance(container, ast.Tuple | ast.List | ast.Set):
            items = list(container.elts)
        else:
            return None
        found: list[str] = []
        for item in items:
            values = None if isinstance(item, ast.Starred) else owner.strings(item, seen)
            if values is None:
                return None
            found.extend(values)
        unique = tuple(dict.fromkeys(found))
        return unique if len(unique) <= _LITERAL_LIMIT else None

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
        if combinations > _LITERAL_LIMIT:
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
