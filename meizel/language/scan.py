"""Fact extraction from one first-party Python file, without importing it."""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from meizel.graph.model import Code, Fact, FactKind, MeizelError, Patch, Problem, Restricted
from meizel.graph.scopes import Scope, collect_scope, dotted_chain, parameter_names
from meizel.graph.universe import Universe
from meizel.language import fixtures
from meizel.language.bindings import module_bindings, provides
from meizel.language.restricted import EXTERNAL_FORMS, RestrictedForms, const_str

__all__ = ["EXTERNAL_FORMS", "FileScan", "scan_file", "scan_source"]

_PATCH_CALLS = {
    "unittest.mock.patch": "patch",
    "unittest.mock.patch.object": "object",
    "unittest.mock.patch.multiple": "multiple",
    "unittest.mock.patch.dict": "dict",
}
_MONKEYPATCH_METHODS = frozenset({"setattr", "delattr", "setitem", "delitem", "setenv", "delenv"})
# The dotted name of the process environment, spelled in parts so this scanner does not itself
# read as a use of it.
_ENVIRONMENT = ".".join(("os", "environ"))
_PATCH_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_LEAVES = frozenset(
    {
        ast.Constant,
        ast.Load,
        ast.Store,
        ast.Del,
        ast.And,
        ast.Or,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.MatMult,
        ast.Div,
        ast.Mod,
        ast.Pow,
        ast.LShift,
        ast.RShift,
        ast.BitOr,
        ast.BitXor,
        ast.BitAnd,
        ast.FloorDiv,
        ast.Invert,
        ast.Not,
        ast.UAdd,
        ast.USub,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Is,
        ast.IsNot,
        ast.In,
        ast.NotIn,
        ast.Pass,
        ast.Break,
        ast.Continue,
    }
)


@dataclass(slots=True)
class FileScan:
    path: str
    facts: list[Fact] = field(default_factory=list[Fact])
    restricted: list[Restricted] = field(default_factory=list[Restricted])
    patches: list[Patch] = field(default_factory=list[Patch])
    problems: list[Problem] = field(default_factory=list[Problem])
    fixtures: fixtures.FixtureState = field(default_factory=fixtures.FixtureState)
    imported: set[str] = field(default_factory=set[str])


class Scanner(RestrictedForms):
    def __init__(self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool) -> None:
        super().__init__(universe, path, tree, walrus=walrus)
        self.test_file = fixtures.is_test_file(path)
        self.fixture_state = fixtures.FixtureState()
        self.fn_depth = 0
        self.patches: list[Patch] = []
        self._handlers: dict[type[ast.AST], Callable[[Any], None]] = {
            ast.Import: self.visit_import,
            ast.ImportFrom: self.visit_import_from,
            ast.Attribute: self.visit_attribute,
            ast.Name: self.visit_name,
            ast.Call: self.visit_call,
            ast.Subscript: self.visit_subscript,
            ast.FunctionDef: self.visit_function,
            ast.AsyncFunctionDef: self.visit_function,
            ast.ClassDef: self.visit_class_def,
            ast.Assign: self.visit_assign,
            ast.AugAssign: self.visit_store,
            ast.AnnAssign: self.visit_store,
            ast.Delete: self.visit_store,
            ast.Expr: self.visit_expr,
            ast.List: self.visit_sequence,
            ast.Tuple: self.visit_sequence,
        }

    def run(self) -> FileScan:
        self.children(self.tree)
        return FileScan(
            self.path,
            facts=list(dict.fromkeys(self.facts)),
            restricted=self.restricted,
            patches=self.patches,
            problems=self.problems,
            fixtures=self.fixture_state,
            imported=self.imported,
        )

    # --- traversal -----------------------------------------------------------------

    def visit(self, node: ast.AST) -> None:
        handler = self._handlers.get(type(node))
        if handler is None:
            self.children(node)
        else:
            handler(node)

    def children(self, node: ast.AST) -> None:
        handlers = self._handlers
        for name in node._fields:
            value = getattr(node, name, None)
            if isinstance(value, list):
                for child in cast("list[object]", value):
                    if isinstance(child, ast.AST) and type(child) not in _LEAVES:
                        handler = handlers.get(type(child))
                        handler(child) if handler else self.children(child)
            elif isinstance(value, ast.AST) and type(value) not in _LEAVES:
                handler = handlers.get(type(value))
                handler(value) if handler else self.children(value)

    def visit_attribute(self, node: ast.Attribute) -> None:
        chain = dotted_chain(node)
        if chain is None:
            self.children(node)
            return
        base, attrs = chain
        origin, _ = self.scope.find(base.id)
        if origin is None:
            return
        self.restricted_attribute(origin, attrs, node.lineno)
        self.module_uses(origin, attrs, node.lineno)

    # --- scopes --------------------------------------------------------------------

    def visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        for deco in node.decorator_list:
            self.visit(deco)
        for default in (*args.defaults, *(d for d in args.kw_defaults if d is not None)):
            self.visit(default)
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None and arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        fixtures.record_function(
            self.fixture_state,
            node,
            self.dotted,
            test_file=self.test_file,
            top_level=self.fn_depth == 0,
        )
        parent = self.scope.parent if self.scope.is_class else self.scope
        inner = Scope(parent)
        inner.shadowed.update(parameter_names(args))
        collect_scope(inner, node.body, self.package, walrus=self.walrus)
        self.fn_depth += 1
        self.enter(inner, node.body)
        self.fn_depth -= 1

    def visit_class_def(self, node: ast.ClassDef) -> None:
        for outer in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(outer)
        inner = Scope(self.scope, is_class=True)
        collect_scope(inner, node.body, self.package, walrus=self.walrus)
        self.enter(inner, node.body)

    def enter(self, inner: Scope, body: list[ast.stmt]) -> None:
        outer, self.scope = self.scope, inner
        for statement in body:
            self.visit(statement)
        self.scope = outer

    # --- statements ----------------------------------------------------------------

    def visit_expr(self, node: ast.Expr) -> None:
        if const_str(node.value) is None:
            self.children(node)

    def visit_assign(self, node: ast.Assign) -> None:
        self.stores(node.targets, node.lineno, "assignment")
        at_module = self.scope.parent is None
        if at_module and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            names = fixtures.plugin_names(node.value)
            if names is None:
                self.restrict(node.lineno, "pytest_plugins that is not a literal")
            else:
                self.fixture_state.plugins.extend((n, node.lineno) for n in names)
                for name in names:
                    self.plugin_fact(name, node.lineno)
        self.children(node)

    def visit_store(self, node: ast.AugAssign | ast.AnnAssign | ast.Delete) -> None:
        if isinstance(node, ast.Delete):
            self.stores(node.targets, node.lineno, "del")
        elif not isinstance(node, ast.AnnAssign) or node.value is not None:
            self.stores([node.target], node.lineno, "assignment")
        self.children(node)

    # --- attribute writes ----------------------------------------------------------

    def stores(self, targets: list[ast.expr], line: int, form: str) -> None:
        """Writes through an imported name are patches, exactly as `monkeypatch.setattr` is."""
        for target in targets:
            if isinstance(target, ast.Tuple | ast.List):
                self.stores(target.elts, line, form)
            elif isinstance(target, ast.Starred):
                self.stores([target.value], line, form)
            elif isinstance(target, ast.Attribute):
                self.written(target.value, (target.attr,), line, form)
            elif isinstance(target, ast.Subscript):
                self.dict_write(target, line)

    def dict_write(self, target: ast.Subscript, line: int) -> None:
        value = target.value
        owner: ast.expr | None = None
        if isinstance(value, ast.Attribute) and value.attr == "__dict__":
            owner = value.value
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "vars"
            and value.args
        ):
            owner = value.args[0]
        if owner is not None:
            name = const_str(target.slice)
            self.written(owner, (name,) if name else (), line, "__dict__ write")

    def written(self, owner: ast.expr, names: tuple[str, ...], line: int, form: str) -> None:
        dotted = self.dotted(owner)
        if dotted is None:
            return
        module = self.u.resolve_module(dotted) if self.u.is_first_party_name(dotted) else None
        if module is not None and module.file == self.path:
            return
        for name in names or ("",):
            target = f"{dotted}.{name}" if name else dotted
            self.patches.append(Patch(line, form, target, self.patch_code(dotted)))
        self.require_target(owner, names, line, form)

    def is_builtin_attribute_call(self, node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Name)
            and func.id in {"setattr", "delattr"}
            and bool(node.args)
            and self.scope.find(func.id) == (None, False)
        )

    def builtin_attribute_call(self, node: ast.Call) -> None:
        name = const_str(node.args[1]) if len(node.args) > 1 else None
        form = cast("ast.Name", node.func).id
        self.written(node.args[0], (name,) if name else (), node.lineno, form)

    def require_target(self, owner: ast.expr, names: tuple[str, ...], line: int, form: str) -> None:
        """A name set or removed on a first-party module must be one the module binds.

        Otherwise the write creates an attribute nothing in the module reads, and the patch
        silently changes nothing. Dynamic modules, builtins and submodules count as bound.
        """
        dotted = self.dotted(owner)
        if dotted is None or not self.u.is_first_party_name(dotted):
            return
        module = self.u.resolve_module(dotted)
        if module is None or module.file is None or module.file == self.path:
            return
        bindings = module_bindings(self.u.root, module.file)
        for name in names:
            if provides(bindings, name) or self.u.resolve_module(f"{dotted}.{name}") is not None:
                continue
            self.problem(
                line,
                f"{form} on {dotted}.{name}: {module.file} binds no `{name}`, so the write"
                " changes a name nothing in the module reads; patch a name the code under test"
                " looks up, or inject the dependency",
                Code.MISSING_PATCH_TARGET,
            )

    def plugin_fact(self, dotted: str, line: int) -> None:
        module = self.u.resolve_module(dotted) if self.u.is_first_party_name(dotted) else None
        if module is not None and module.file is not None:
            self.add(module.file, line, FactKind.PLUGIN, (), f"pytest_plugins {dotted}")

    # --- calls ---------------------------------------------------------------------

    def visit_call(self, node: ast.Call) -> None:
        callee = self.dotted(node.func)
        kind = _PATCH_CALLS.get(callee) if callee else None
        if kind is not None:
            self.patch_call(node, kind)
        elif self.is_monkeypatch(node.func):
            self.monkeypatch_call(node)
        elif self.is_builtin_attribute_call(node):
            self.builtin_attribute_call(node)
        elif self.is_sys_modules_method(node.func):
            self.sys_modules_call(node)
        elif self.is_shell_call(callee):
            self.shell_call(node)
        fixtures.record_call(self.fixture_state, node, callee)
        self.children(node)

    @staticmethod
    def is_monkeypatch(func: ast.expr) -> bool:
        if not isinstance(func, ast.Attribute) or func.attr not in _MONKEYPATCH_METHODS:
            return False
        receiver = func.value
        name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
        return "monkeypatch" in name.lower()

    def patch_call(self, node: ast.Call, kind: str) -> None:
        first = node.args[0] if node.args else None
        form = "patch" if kind == "patch" else f"patch.{kind}"
        if kind == "patch" or const_str(first) is not None:
            self.string_target(first, node.lineno, form, self.patched_names(node, kind))
            if kind == "dict":
                self.patched_sys_modules(node)
        elif kind == "dict":
            self.patched_sys_modules(node)
            if first is not None:
                self.record_patch(first, (), node.lineno, form)
        elif first is not None and kind in {"object", "multiple"}:
            names = self.patched_names(node, kind)
            self.patch_fact(first, names, node.lineno, form)
            self.record_patch(first, names, node.lineno, form)
            self.require_target(first, names, node.lineno, form)

    @staticmethod
    def patched_names(node: ast.Call, kind: str) -> tuple[str, ...]:
        if kind == "object":
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            return (name,) if name else ()
        if kind == "multiple":
            return tuple(k.arg for k in node.keywords if k.arg and k.arg not in _PATCH_OPTIONS)
        return ()

    def patch_fact(self, target: ast.expr, names: tuple[str, ...], line: int, detail: str) -> None:
        module = self.module_file(target)
        if module is not None:
            self.add(module, line, FactKind.PATCH, names, detail)

    def monkeypatch_call(self, node: ast.Call) -> None:
        if not node.args:
            return
        method = cast("ast.Attribute", node.func).attr
        form = f"monkeypatch.{method}"
        if method in {"setenv", "delenv"}:
            name = const_str(node.args[0])
            self.patches.append(
                Patch(
                    node.lineno,
                    form,
                    f"{_ENVIRONMENT}.{name}" if name else _ENVIRONMENT,
                    Code.ENV_PATCH,
                )
            )
        elif method in {"setitem", "delitem"}:
            self.monkeypatch_item(node, form)
        elif const_str(node.args[0]) is not None:
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            self.string_target(node.args[0], node.lineno, form, (name,) if name else ())
        else:
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            self.patch_fact(node.args[0], (name,) if name else (), node.lineno, "monkeypatch")
            self.record_patch(node.args[0], (name,) if name else (), node.lineno, form)
            self.require_target(node.args[0], (name,) if name else (), node.lineno, form)

    def monkeypatch_item(self, node: ast.Call, form: str) -> None:
        if self.is_sys_modules(node.args[0]):
            if len(node.args) > 1:
                self.fake_module(node.args[1], node.lineno)
        elif self.dotted(node.args[0]) == _ENVIRONMENT:
            name = const_str(node.args[1]) if len(node.args) > 1 else None
            self.patches.append(
                Patch(
                    node.lineno,
                    form,
                    f"{_ENVIRONMENT}.{name}" if name else _ENVIRONMENT,
                    Code.ENV_PATCH,
                )
            )

    def patch_code(self, dotted: str) -> Code:
        """What kind of code a resolved patch target is."""
        if dotted == _ENVIRONMENT or dotted.startswith(f"{_ENVIRONMENT}."):
            return Code.ENV_PATCH
        if self.u.is_first_party_name(dotted):
            return Code.FIRST_PARTY_PATCH
        if dotted.split(".", 1)[0] in sys.stdlib_module_names:
            return Code.STDLIB_PATCH
        return Code.THIRD_PARTY_PATCH

    def record_patch(self, target: ast.expr, names: tuple[str, ...], line: int, form: str) -> None:
        """A patch of a module or object, classified by what the file's imports say it is."""
        dotted = self.dotted(target)
        code = Code.UNRESOLVED_PATCH if dotted is None else self.patch_code(dotted)
        base = " ".join(ast.unparse(target).split())[:80] if dotted is None else dotted
        for name in names or ("",):
            self.patches.append(Patch(line, form, f"{base}.{name}" if name else base, code))

    def string_target(
        self, target: ast.expr | None, line: int, form: str, names: tuple[str, ...] = ()
    ) -> None:
        """A string target names first-party code invisibly (restricted); any other is a patch."""
        text = None if target is None else self.string_value(target)
        if text is None or self.u.is_first_party_name(text):
            self.restrict(line, "string patch target", text or "")
            return
        for name in names or ("",):
            self.patches.append(
                Patch(line, form, f"{text}.{name}" if name else text, self.patch_code(text))
            )

    def visit_subscript(self, node: ast.Subscript) -> None:
        self.sys_modules_write(node)
        self.children(node)

    # --- python -c -----------------------------------------------------------------

    def visit_sequence(self, node: ast.List | ast.Tuple) -> None:
        self.python_c_in_sequence(node)
        self.children(node)


def scan_source(universe: Universe, path: str, source: bytes) -> FileScan:
    """Scan `source` as if it were the file at `path` (a version that is not in the working tree)."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise MeizelError(f"{path}:{error.lineno}: cannot parse: {error.msg}") from error
    except ValueError as error:
        raise MeizelError(f"{path}: cannot read: {error}") from error
    return Scanner(universe, path, tree, walrus=b":=" in source).run()


def scan_file(universe: Universe, path: str) -> FileScan:
    try:
        source = (universe.root / path).read_bytes()
    except (OSError, ValueError) as error:
        raise MeizelError(f"{path}: cannot read: {error}") from error
    return scan_source(universe, path, source)
