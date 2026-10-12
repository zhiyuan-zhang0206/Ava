"""Fact extraction from one first-party Python file, without importing it."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from scripts.meizel import fixtures
from scripts.meizel.model import Code, Fact, FactKind, MeizelError, Problem, Restricted
from scripts.meizel.scopes import (
    Scope,
    absolute_base,
    collect_scope,
    dotted_chain,
    parameter_names,
)
from scripts.meizel.universe import Universe

__all__ = ["EXTERNAL_FORMS", "FileScan", "scan_file"]

_RESTRICTED_REFS = {
    "importlib.import_module": "importlib.import_module",
    "runpy.run_module": "runpy.run_module",
    "runpy.run_path": "runpy.run_path",
    "importlib.util.spec_from_file_location": "importlib.util.spec_from_file_location",
    "builtins.__import__": "__import__",
    "builtins.exec": "exec of source text",
    "builtins.compile": "compile of source text",
}
# The forms a file may use once `[external]` declares that it runs code from outside the repository.
EXTERNAL_FORMS = frozenset(
    {
        "importlib.import_module",
        "runpy.run_module",
        "runpy.run_path",
        "importlib.util.spec_from_file_location",
        "__import__",
        "exec of source text",
        "compile of source text",
    }
)
_BUILTIN_REFS = frozenset({"__import__", "exec", "compile"})
_PATCH_CALLS = {
    "unittest.mock.patch": "patch",
    "unittest.mock.patch.object": "object",
    "unittest.mock.patch.multiple": "multiple",
    "unittest.mock.patch.dict": "dict",
}
_MONKEYPATCH_METHODS = frozenset({"setattr", "delattr", "setitem", "delitem"})
_SYS_MODULES_METHODS = frozenset({"setdefault", "pop", "update"})
_PATCH_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_SHELL_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
    }
)
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
_IMPORT_IN_SOURCE = re.compile(
    r"""(?:^|[;\n("'])[ \t]*(?:from|import)[ \t]+([A-Za-z_]\w*)""", re.MULTILINE
)
_SHELL_PYTHON_C = re.compile(r"\bpython[\d.]*\s+(?:-\S+\s+)*-c\s")


@dataclass(slots=True)
class FileScan:
    path: str
    facts: list[Fact] = field(default_factory=list[Fact])
    restricted: list[Restricted] = field(default_factory=list[Restricted])
    problems: list[Problem] = field(default_factory=list[Problem])
    fixtures: fixtures.FixtureState = field(default_factory=fixtures.FixtureState)
    imported: set[str] = field(default_factory=set[str])


def _const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class Scanner:
    def __init__(self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool) -> None:
        self.u = universe
        self.path = path
        self.tree = tree
        self.package = path.split("/")[:-1]
        self.test_file = fixtures.is_test_file(path)
        self.scan = FileScan(path)
        self.walrus = walrus
        self.scope = Scope(None)
        collect_scope(self.scope, tree.body, self.package, walrus=walrus)
        self.scan.imported = set(self.scope.imports)
        self.fn_depth = 0
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
            ast.Expr: self.visit_expr,
            ast.List: self.visit_sequence,
            ast.Tuple: self.visit_sequence,
        }

    def run(self) -> FileScan:
        self.children(self.tree)
        self.scan.facts = list(dict.fromkeys(self.scan.facts))
        return self.scan

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

    def add(self, dst: str, line: int, kind: FactKind, names: tuple[str, ...], detail: str) -> None:
        self.scan.facts.append(Fact(self.path, dst, line, kind, names, detail))

    def restrict(self, line: int, form: str, detail: str = "") -> None:
        self.scan.restricted.append(Restricted(line, form, detail))

    def problem(self, line: int, message: str) -> None:
        self.scan.problems.append(Problem(line, Code.UNRESOLVED_IMPORT, message))

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

    # --- names and attributes ------------------------------------------------------

    def visit_name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        origin, bound = self.scope.find(node.id)
        if origin is not None:
            form = _RESTRICTED_REFS.get(origin)
            if form:
                self.restrict(node.lineno, form)
        elif not bound and node.id in _BUILTIN_REFS:
            self.restrict(node.lineno, _RESTRICTED_REFS[f"builtins.{node.id}"])

    def visit_attribute(self, node: ast.Attribute) -> None:
        chain = dotted_chain(node)
        if chain is None:
            self.children(node)
            return
        base, attrs = chain
        origin, _ = self.scope.find(base.id)
        if origin is None:
            return
        parts = [origin, *attrs]
        for end in range(1, len(parts) + 1):
            form = _RESTRICTED_REFS.get(".".join(parts[:end]))
            if form:
                self.restrict(node.lineno, form)
        self.module_uses(origin, attrs, node.lineno)

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
            self.scan.fixtures,
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
        if _const_str(node.value) is None:
            self.children(node)

    def visit_assign(self, node: ast.Assign) -> None:
        at_module = self.scope.parent is None
        if at_module and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            names = fixtures.plugin_names(node.value)
            if names is None:
                self.restrict(node.lineno, "pytest_plugins that is not a literal")
            else:
                self.scan.fixtures.plugins.extend((n, node.lineno) for n in names)
                for name in names:
                    self.plugin_fact(name, node.lineno)
        self.children(node)

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
        elif self.is_sys_modules_method(node.func):
            self.sys_modules_call(node)
        elif callee in _SHELL_CALLS:
            self.shell_call(node)
        fixtures.record_call(self.scan.fixtures, node, callee)
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
        if kind == "patch" or _const_str(first) is not None:
            self.string_target(first, node.lineno)
            if kind == "dict":
                self.patched_sys_modules(node)
        elif kind == "dict":
            self.patched_sys_modules(node)
        elif first is not None and kind in {"object", "multiple"}:
            names = self.patched_names(node, kind)
            self.patch_fact(first, names, node.lineno, f"patch.{kind}")

    @staticmethod
    def patched_names(node: ast.Call, kind: str) -> tuple[str, ...]:
        if kind == "object":
            name = _const_str(node.args[1]) if len(node.args) > 1 else None
            return (name,) if name else ()
        return tuple(k.arg for k in node.keywords if k.arg and k.arg not in _PATCH_OPTIONS)

    def patch_fact(self, target: ast.expr, names: tuple[str, ...], line: int, detail: str) -> None:
        module = self.module_file(target)
        if module is not None:
            self.add(module, line, FactKind.PATCH, names, detail)

    def monkeypatch_call(self, node: ast.Call) -> None:
        if not node.args:
            return
        method = cast("ast.Attribute", node.func).attr
        if method in {"setitem", "delitem"}:
            if self.is_sys_modules(node.args[0]) and len(node.args) > 1:
                self.fake_module(node.args[1], node.lineno)
            return
        if _const_str(node.args[0]) is not None:
            self.string_target(node.args[0], node.lineno)
            return
        name = _const_str(node.args[1]) if len(node.args) > 1 else None
        self.patch_fact(node.args[0], (name,) if name else (), node.lineno, "monkeypatch")

    def string_target(self, target: ast.expr | None, line: int) -> None:
        """A string patch target hides a dependency only when it names first-party code."""
        text = None if target is None else self.string_value(target)
        if text is None or self.u.is_first_party_name(text):
            self.restrict(line, "string patch target", text or "")

    # --- sys.modules ---------------------------------------------------------------

    def is_sys_modules(self, expr: ast.expr) -> bool:
        return self.dotted(expr) == "sys.modules"

    def is_sys_modules_method(self, func: ast.expr) -> bool:
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _SYS_MODULES_METHODS
            and self.is_sys_modules(func.value)
        )

    def fake_module(self, key: ast.expr, line: int) -> None:
        """Writing a first-party module into `sys.modules` by a literal name. Reads are fine."""
        text = _const_str(key)
        if text is not None and self.u.is_first_party_name(text):
            self.restrict(line, "sys.modules write of a first-party module", text)

    def sys_modules_call(self, node: ast.Call) -> None:
        if not node.args:
            for keyword in node.keywords:
                self.fake_module(ast.Constant(keyword.arg), node.lineno)
            return
        first = node.args[0]
        if isinstance(first, ast.Dict):
            for key in first.keys:
                if key is not None:
                    self.fake_module(key, node.lineno)
        else:
            self.fake_module(first, node.lineno)

    def visit_subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del) and self.is_sys_modules(node.value):
            self.fake_module(node.slice, node.lineno)
        self.children(node)

    def patched_sys_modules(self, node: ast.Call) -> None:
        """`patch.dict(sys.modules, {...})` (or the string form) with first-party keys."""
        first = node.args[0] if node.args else None
        if first is None or not (self.is_sys_modules(first) or _const_str(first) == "sys.modules"):
            return
        values = node.args[1] if len(node.args) > 1 else None
        if isinstance(values, ast.Dict):
            for key in values.keys:
                if key is not None:
                    self.fake_module(key, node.lineno)
        for keyword in node.keywords:
            self.fake_module(ast.Constant(keyword.arg), node.lineno)

    def shell_call(self, node: ast.Call) -> None:
        text = _const_str(node.args[0]) if node.args else None
        if text is not None and _SHELL_PYTHON_C.search(text):
            self.check_source(text, node.lineno)

    # --- python -c -----------------------------------------------------------------

    def visit_sequence(self, node: ast.List | ast.Tuple) -> None:
        for index, element in enumerate(node.elts[:4]):
            if index and _const_str(element) == "-c" and index + 1 < len(node.elts):
                if self.looks_like_python(node.elts[0]):
                    self.python_c(node.elts[index + 1], node.lineno)
                break
        self.children(node)

    @staticmethod
    def looks_like_python(command: ast.expr) -> bool:
        text = ast.unparse(command).lower()
        return "python" in text or "executable" in text

    def python_c(self, source: ast.expr, line: int) -> None:
        text = self.string_value(source)
        if text is None:
            self.restrict(line, "python -c source that is not a literal")
        else:
            self.check_source(text, line)

    def check_source(self, text: str, line: int) -> None:
        if any(self.u.is_first_party_name(m) for m in _IMPORT_IN_SOURCE.findall(text)):
            self.restrict(line, "python -c source that imports first-party code")

    def string_value(self, node: ast.expr, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return self.joined_value(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.string_value(node.left, depth + 1)
            right = self.string_value(node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            value = self.scope.constant(node.id)
            return None if value is None else self.string_value(value, depth + 1)
        return None

    @staticmethod
    def joined_value(node: ast.JoinedStr) -> str | None:
        """An f-string's literal text, with each interpolation reduced to `{}`."""
        parts = ["{}" if isinstance(v, ast.FormattedValue) else _const_str(v) for v in node.values]
        return None if None in parts else "".join(p for p in parts if p is not None)


def scan_file(universe: Universe, path: str) -> FileScan:
    full = universe.root / path
    try:
        source = full.read_bytes()
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        raise MeizelError(f"{path}:{error.lineno}: cannot parse: {error.msg}") from error
    except (OSError, ValueError) as error:
        raise MeizelError(f"{path}: cannot read: {error}") from error
    return Scanner(universe, path, tree, walrus=b":=" in source).run()
