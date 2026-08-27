"""AST extraction of ``ava.*`` SDK call sites from agent code blocks.

Owned by the timeline renderer (``shared/timeline.py`` imports ``SdkCall`` and
``extract_sdk_calls`` from here); the extracted counts drive the collapsed-code
chip ("files.read x3" etc.) with zero false positives from string literals or
comments.

The extractor resolves names through the snippet's own imports, so every
import style counts::

    import ava                          ->  ava.shell.run(...)      "shell.run"
    import ava.shell as sh              ->  sh.run(...)             "shell.run"
    from ava import shell               ->  shell.run(...)          "shell.run"
    from ava import shell as sh         ->  sh.run(...)             "shell.run"
    from ava.shell import run           ->  run(...)                "shell.run"
    from ava import shell, files        ->  shell.run(...) + files.read(...)

A name bound to something other than the SDK module never counts, even when an
import of the same name exists elsewhere in the snippet: an assignment,
parameter, or for/with/except/comprehension target shadows the import from its
point in the source onward (scope-aware, source-ordered). A bare ``ava.`` root
still counts when ``ava`` is never bound in the snippet (the import may live in
an earlier cell), matching the historical literal-prefix behavior.
"""

from __future__ import annotations

import ast as _ast

from pydantic import BaseModel


class SdkCall(BaseModel):
    """One SDK method's call count in an agent_code block, e.g.
    ``SdkCall(method="files.read", count=3)`` for ``ava.files.read(...)`` x3."""

    method: str
    count: int


class _Unbound:
    """Sentinel type: a name never bound anywhere in the snippet.

    Distinct from a binding of ``None`` (bound to something that is not the
    SDK module) so the bare-``ava`` fallback can tell "no import seen" from
    "``ava`` explicitly shadowed".
    """


_UNBOUND = _Unbound()


class _Scope:
    """One lexical scope: name bindings plus the parent chain for lookup.

    ``is_class`` marks class bodies: a function body never sees names bound in
    an enclosing class body (LEGB skips class scopes for method bodies), so
    function scopes link past class scopes to the nearest enclosing
    function/module scope, while class bodies link to their immediate
    enclosing scope.
    """

    __slots__ = ("bindings", "is_class", "parent")

    def __init__(self, *, is_class: bool = False, parent: _Scope | None = None) -> None:
        self.bindings: dict[str, tuple[str, ...] | None] = {}
        self.is_class = is_class
        self.parent = parent


class _SdkCallVisitor(_ast.NodeVisitor):
    """Collect ``ava`` SDK call sites, resolving names through import bindings.

    Walks the tree in source order so a binding applies only from its
    definition point on: ``from ava import shell`` makes ``shell.run(...)``
    count, a later ``shell = other`` makes it stop counting. Import forms bind
    names to SDK path prefixes (``from ava.shell import run`` binds ``run`` to
    ``("shell", "run")``); every other binding (assignment, parameter, loop /
    with / except / comprehension target) binds the name to ``None`` — it
    shadows any import of the same name and never counts.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self._scope = _Scope()  # module scope; never popped
        self._saved: list[_Scope] = []  # pre-push scopes, for balanced pops

    # --- name binding -------------------------------------------------------

    def _bind(self, name: str, path: tuple[str, ...] | None) -> None:
        self._scope.bindings[name] = path

    def _push_scope(self, *, is_class: bool = False) -> None:
        previous = self._scope
        if is_class:
            parent = previous
        else:
            # Function / lambda / comprehension bodies skip class scopes (LEGB).
            parent = previous
            while parent.is_class and parent.parent is not None:
                parent = parent.parent
        self._saved.append(previous)
        self._scope = _Scope(is_class=is_class, parent=parent)

    def _pop_scope(self) -> None:
        # Pop restores the scope current before the matching push — NOT the
        # new scope's parent: a function scope's parent may skip class scopes.
        self._scope = self._saved.pop()

    def _binding(self, name: str) -> tuple[str, ...] | None | _Unbound:
        """The innermost visible binding of *name*, or ``_UNBOUND``."""
        scope: _Scope | None = self._scope
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return _UNBOUND

    def _bind_targets(self, node: _ast.expr | None) -> None:
        """Bind every plain name in an assignment target to ``None`` (shadowed)."""
        if node is None:
            return
        if isinstance(node, _ast.Name):
            self._bind(node.id, None)
        elif isinstance(node, (_ast.Tuple, _ast.List)):
            for elt in node.elts:
                self._bind_targets(elt)
        # Subscript / attribute targets do not bind a plain name.

    # --- statements that bind or shadow names ------------------------------

    def visit_Import(self, node: _ast.Import) -> None:
        for alias in node.names:
            if alias.name == "ava" or alias.name.startswith("ava."):
                if alias.asname is not None:
                    # `import ava.shell as sh` binds `sh` to ava.shell.
                    self._bind(alias.asname, tuple(alias.name.split(".")[1:]))
                else:
                    # `import ava` / `import ava.shell` bind the top-level name `ava`.
                    self._bind("ava", ())
            else:
                self._bind(alias.asname or alias.name.split(".")[0], None)

    def visit_ImportFrom(self, node: _ast.ImportFrom) -> None:
        if node.module == "ava":
            prefix: tuple[str, ...] | None = ()
        elif node.module is not None and node.module.startswith("ava."):
            prefix = tuple(node.module.split(".")[1:])
        else:
            prefix = None  # not the SDK at all — every imported name is shadowed
        for alias in node.names:
            if alias.name == "*":
                continue  # a wildcard import cannot be resolved statically
            name = alias.asname or alias.name
            if prefix is None:
                self._bind(name, None)
            else:
                self._bind(name, (*prefix, alias.name))

    def visit_Assign(self, node: _ast.Assign) -> None:
        self.visit(node.value)  # the RHS evaluates with the old bindings
        for target in node.targets:
            self._bind_targets(target)

    def visit_AnnAssign(self, node: _ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)
        self._bind_targets(node.target)

    def visit_AugAssign(self, node: _ast.AugAssign) -> None:
        self.visit(node.value)  # the RHS evaluates with the old bindings
        self._bind_targets(node.target)

    def visit_NamedExpr(self, node: _ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_targets(node.target)

    def _visit_for(self, node: _ast.For | _ast.AsyncFor) -> None:
        self.visit(node.iter)  # the iterable evaluates with the old bindings
        self._bind_targets(node.target)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_For(self, node: _ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: _ast.AsyncFor) -> None:
        self._visit_for(node)  # same shape as For

    def _visit_with(self, node: _ast.With | _ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)  # the context expr evaluates first
            self._bind_targets(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)

    def visit_With(self, node: _ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: _ast.AsyncWith) -> None:
        self._visit_with(node)  # same shape as With

    def visit_ExceptHandler(self, node: _ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._bind(node.name, None)  # `except E as shell:` shadows
        for stmt in node.body:
            self.visit(stmt)

    # --- scoped statements ---------------------------------------------------

    def _visit_function(self, node: _ast.FunctionDef | _ast.AsyncFunctionDef) -> None:
        # Decorators, annotations, defaults, and type params evaluate in the
        # ENCLOSING scope.
        for dec in node.decorator_list:
            self.visit(dec)
        if node.returns is not None:
            self.visit(node.returns)
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        for vararg in (args.vararg, args.kwarg):
            if vararg is not None and vararg.annotation is not None:
                self.visit(vararg.annotation)
        for default in [*args.defaults, *args.kw_defaults]:
            if default is not None:
                self.visit(default)
        for tp in node.type_params:
            self.visit(tp)
        self._push_scope()
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self._bind(arg.arg, None)  # parameters shadow any import
        for vararg in (args.vararg, args.kwarg):
            if vararg is not None:
                self._bind(vararg.arg, None)
        for stmt in node.body:
            self.visit(stmt)
        self._pop_scope()

    def visit_FunctionDef(self, node: _ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: _ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: _ast.Lambda) -> None:
        args = node.args
        for default in [*args.defaults, *args.kw_defaults]:
            if default is not None:
                self.visit(default)
        self._push_scope()
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self._bind(arg.arg, None)
        for vararg in (args.vararg, args.kwarg):
            if vararg is not None:
                self._bind(vararg.arg, None)
        self.visit(node.body)
        self._pop_scope()

    def visit_ClassDef(self, node: _ast.ClassDef) -> None:
        # Decorators, bases, keywords, and type params evaluate in the ENCLOSING scope.
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        for tp in node.type_params:
            self.visit(tp)
        self._push_scope(is_class=True)
        for stmt in node.body:
            self.visit(stmt)
        self._pop_scope()

    def _visit_comprehension(
        self, node: _ast.ListComp | _ast.SetComp | _ast.DictComp | _ast.GeneratorExp
    ) -> None:
        gens = node.generators
        self._push_scope()
        self.visit(gens[0].iter)  # the first iterable evaluates in the ENCLOSING scope
        for gen in gens:
            self._bind_targets(gen.target)  # comp targets shadow within the comp
        for expr in gens[0].ifs:
            self.visit(expr)
        for gen in gens[1:]:
            self.visit(gen.iter)  # later iterables see the comp scope (Python semantics)
            for expr in gen.ifs:
                self.visit(expr)
        if isinstance(node, _ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self._pop_scope()

    def visit_ListComp(self, node: _ast.ListComp) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node: _ast.SetComp) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node: _ast.DictComp) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: _ast.GeneratorExp) -> None:
        self._visit_comprehension(node)

    # --- call resolution ------------------------------------------------------

    def visit_Call(self, node: _ast.Call) -> None:
        method = self._resolve(node.func)
        if method is not None:
            self.counts[method] = self.counts.get(method, 0) + 1
        self.generic_visit(node)  # nested calls inside the arguments still count

    def _resolve(self, node: _ast.expr) -> str | None:
        """The SDK method path when *node* is a callable rooted in the ava SDK.

        Walks an ``ast.Attribute`` chain down to its root name, then resolves
        that name through the visible import bindings::

            ava.files.read(...)   ->  root "ava" unbound -> fallback () -> "files.read"
            sh.run(...)           ->  binding ("shell",) -> "shell.run"
            run(...)              ->  binding ("shell", "run") -> "shell.run"

        A root name bound to ``None`` (shadowed or a non-SDK import) never
        counts; a root name never bound in the snippet counts only when it is
        ``ava`` itself (the import may live in an earlier cell).
        """
        parts: list[str] = []
        current: _ast.expr = node
        while isinstance(current, _ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, _ast.Name):
            return None
        binding = self._binding(current.id)
        if binding is None:
            return None
        if isinstance(binding, _Unbound):
            if current.id != "ava":
                return None
            path: tuple[str, ...] = ()
        else:
            path = binding
        method_parts = (*path, *reversed(parts))
        return ".".join(method_parts) if method_parts else None


def extract_sdk_calls(code: str) -> list[SdkCall]:
    """Parse Python code with ``ast`` and return the SDK calls it contains.

    Call sites are resolved through the snippet's own imports (scope-aware,
    source-ordered), so ``from ava import shell`` + ``shell.run(...)`` counts
    exactly like ``ava.shell.run(...)``, and a local name that shadows an
    import never counts. Returns an empty list on syntax errors (streaming
    partial code, invalid Python) — the frontend falls back to regex for those
    items.
    """
    if not code.strip():
        return []
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return []
    visitor = _SdkCallVisitor()
    visitor.visit(tree)
    return [
        SdkCall(method=m, count=c)
        for m, c in sorted(visitor.counts.items(), key=lambda x: (-x[1], x[0]))
    ]
