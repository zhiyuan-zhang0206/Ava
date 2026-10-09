"""Bounded evidence for source passed to an actual Python ``-c`` launch.

Only literal inputs, one plain binding and a local transparent helper are
followed. Other execution inputs stay unresolved; this is not a Python evaluator.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import normalize

_LAUNCHERS = frozenset(
    {f"subprocess.{name}" for name in ("run", "Popen", "call", "check_call", "check_output")}
    | {"asyncio.create_subprocess_exec"}
)
_DYNAMIC_IMPORTS = frozenset({"importlib.import_module", "runpy.run_module", "__import__"})


@dataclass(frozen=True)
class Unresolved:
    """An execution input whose dependency facts are not statically established."""

    path: str
    line: int
    reason: str


@dataclass(frozen=True)
class Source:
    """Source passed to Python, located at its launch site in the owning file."""

    line: int
    text: str


@dataclass
class Inputs:
    """Known execution sources and retained incomplete evidence."""

    sources: list[Source] = field(default_factory=list[Source])
    unresolved: list[Unresolved] = field(default_factory=list[Unresolved])


def _local_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Walk this lexical scope, keeping nested scope bodies out of its bindings."""
    for child in ast.iter_child_nodes(tree):
        yield child
        if not isinstance(
            child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        ):
            yield from _local_nodes(child)


class _Scope:
    def __init__(self, tree: ast.AST, path: str, parent: _Scope | None = None) -> None:
        self.parent = parent
        self.is_class = isinstance(tree, ast.ClassDef)
        self.values: dict[str, ast.expr] = {}
        self.origins: dict[str, str] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.stores: Counter[str] = Counter()
        for node in _local_nodes(tree):
            self._bind(node, path)
        if isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = tree.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self.stores[arg.arg] += 1
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    self.stores[arg.arg] += 1

    def _bind(self, node: ast.AST, path: str) -> None:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self.stores[node.id] += 1
        elif isinstance(node, ast.Assign):
            self._assignment(node.targets, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            self.values[node.target.id] = node.value
        elif isinstance(node, ast.Import | ast.ImportFrom):
            self._import(node, path)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            self.functions[node.name] = node
            self.stores[node.name] += 1
        elif isinstance(node, ast.ClassDef):
            self.stores[node.name] += 1

    def _assignment(self, targets: list[ast.expr], value: ast.expr) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                self.values[target.id] = value

    def _import(self, node: ast.Import | ast.ImportFrom, path: str) -> None:
        if isinstance(node, ast.ImportFrom) and node.level and not path:
            return
        for name, origin in normalize(node, path).origins.items():
            self.origins[name] = origin
            self.stores[name] += 1

    def value(self, node: ast.expr) -> ast.expr:
        """One unambiguous plain binding; parameters and rebinding remain opaque."""
        if not isinstance(node, ast.Name):
            return node
        if node.id not in self.stores and self.parent is not None:
            return self.parent.value(node)
        return self.values.get(node.id, node) if self.stores[node.id] == 1 else node

    def origin(self, node: ast.expr) -> str:
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value)
            return f"{base}.{node.attr}" if base else ""
        if not isinstance(node, ast.Name):
            return ""
        if node.id not in self.stores and self.parent is not None:
            return self.parent.origin(node)
        if node.id == "__import__" and node.id not in self.stores:
            return node.id
        return self.origins.get(node.id, "") if self.stores[node.id] == 1 else ""

    def function(self, name: str) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, _Scope] | None:
        if name not in self.stores and self.parent is not None:
            return self.parent.function(name)
        node = self.functions.get(name)
        return (node, self) if node is not None and self.stores[name] == 1 else None

    def nested_parent(self) -> _Scope:
        scope = self
        while scope.is_class and scope.parent is not None:
            scope = scope.parent
        return scope


def _argv(call: ast.Call, scope: _Scope) -> list[ast.expr] | None:
    if scope.origin(call.func) == "asyncio.create_subprocess_exec":
        return call.args
    first = (
        call.args[0]
        if call.args
        else next((kw.value for kw in call.keywords if kw.arg == "args"), None)
    )
    if first is None:
        return None
    value = scope.value(first)
    return value.elts if isinstance(value, ast.List | ast.Tuple) else None


def _literal_text(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _python_argv(call: ast.Call, scope: _Scope) -> list[ast.expr] | None:
    argv = _argv(call, scope)
    if not argv or scope.origin(scope.value(argv[0])) != "sys.executable":
        return None
    return argv if any(_literal_text(scope.value(item)) == "-c" for item in argv[1:]) else None


def _code_input(call: ast.Call, scope: _Scope) -> tuple[ast.expr | None, str | None]:
    """Locate -c without confusing option operands or trailing argv data with source."""
    argv = _python_argv(call, scope)
    if argv is None:
        return None, None
    offset = 1
    while offset < len(argv):
        flag = _literal_text(scope.value(argv[offset]))
        if flag is None:
            return None, "Python interpreter options are not literal"
        if flag == "-c":
            if offset + 1 < len(argv):
                return argv[offset + 1], None
            return None, "Python -c argv has no source argument"
        if flag in {"-W", "-X"}:
            operand = scope.value(argv[offset + 1]) if offset + 1 < len(argv) else None
            if _literal_text(operand) is None:
                return None, "Python interpreter option operands are not literal"
            offset += 2
        elif not flag.startswith("-") or flag in {"-m", "--"}:
            return None, None
        else:
            offset += 1
    return None, None


def _helper_parameter(
    node: ast.FunctionDef | ast.AsyncFunctionDef, parent: _Scope, path: str
) -> str | None:
    if node.decorator_list:
        return None
    scope = _Scope(node, path, parent.nested_parent())
    launches = [
        call
        for call in _local_nodes(node)
        if isinstance(call, ast.Call) and scope.origin(call.func) in _LAUNCHERS
    ]
    if len(launches) != 1:
        return None
    code, _reason = _code_input(launches[0], scope)
    if code is not None:
        code = scope.value(code)
    parameters = {a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
    if isinstance(code, ast.Name) and code.id in parameters and scope.stores[code.id] == 1:
        return code.id
    return None


def _argument(
    call: ast.Call, function: ast.FunctionDef | ast.AsyncFunctionDef, parameter: str
) -> ast.expr | None:
    """Resolve the source slot; a following *argv does not change earlier arguments."""
    if any(keyword.arg is None for keyword in call.keywords):
        return None
    names = [arg.arg for arg in [*function.args.posonlyargs, *function.args.args]]
    offset = names.index(parameter) if parameter in names else len(call.args)
    starred = _starred_offset(call)
    keyword = next((kw.value for kw in call.keywords if kw.arg == parameter), None)
    if keyword is not None:
        return _source_keyword(
            function,
            parameter,
            keyword,
            ambiguous_positional=starred < len(call.args) or offset < len(call.args),
        )
    if parameter in names and offset < starred:
        return call.args[offset]
    if starred < len(call.args) and parameter in names:
        return None
    return _literal_default(function, parameter, names)


def _starred_offset(call: ast.Call) -> int:
    return next(
        (i for i, arg in enumerate(call.args) if isinstance(arg, ast.Starred)), len(call.args)
    )


def _source_keyword(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter: str,
    value: ast.expr,
    *,
    ambiguous_positional: bool,
) -> ast.expr | None:
    if parameter in {arg.arg for arg in function.args.posonlyargs}:
        return None
    if parameter in {arg.arg for arg in function.args.args} and ambiguous_positional:
        return None
    return value


def _literal_default(
    function: ast.FunctionDef | ast.AsyncFunctionDef, parameter: str, names: list[str]
) -> ast.Constant | None:
    defaults = [
        *zip(
            names[len(names) - len(function.args.defaults) :], function.args.defaults, strict=True
        ),
        *zip((arg.arg for arg in function.args.kwonlyargs), function.args.kw_defaults, strict=True),
    ]
    for name, value in defaults:
        if name == parameter and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value
    return None


class _Inputs(ast.NodeVisitor):
    def __init__(self, tree: ast.AST, path: str) -> None:
        self.path = path
        self.scope = _Scope(tree, path)
        self.result = Inputs()
        self._template_launches: dict[int, tuple[int, ast.Call]] = {}
        self._used_functions: set[int] = set()

    def _nested(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        parent = self.scope
        lexical_parent = parent.nested_parent()
        self.scope = _Scope(node, self.path, lexical_parent)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            parameter = _helper_parameter(node, lexical_parent, self.path)
            if parameter is not None:
                self._template_launches.update(
                    (id(call), (id(node), call))
                    for call in _local_nodes(node)
                    if isinstance(call, ast.Call) and self.scope.origin(call.func) in _LAUNCHERS
                )
        self.generic_visit(node)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        parent = self.scope
        self.scope = _Scope(node, self.path, parent.nested_parent())
        self.generic_visit(node)
        self.scope = parent

    def visit_Call(self, node: ast.Call) -> None:
        if self.scope.origin(node.func) in _LAUNCHERS and id(node) not in self._template_launches:
            code, reason = _code_input(node, self.scope)
            if code is not None:
                self._source(node, code)
            elif reason is not None:
                self.result.unresolved.append(Unresolved(self.path, node.lineno, reason))
        elif isinstance(node.func, ast.Name):
            found = self.scope.function(node.func.id)
            if found is not None:
                function, parent = found
                parameter = _helper_parameter(function, parent, self.path)
                if parameter is not None:
                    self._used_functions.add(id(function))
                    code = _argument(node, function, parameter)
                    if code is not None:
                        self._source(node, code)
                    else:
                        self.result.unresolved.append(
                            Unresolved(
                                self.path, node.lineno, "Python -c helper has no source argument"
                            )
                        )
        self.generic_visit(node)

    def _source(self, call: ast.Call, node: ast.expr) -> None:
        value = self.scope.value(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            self.result.sources.append(Source(call.lineno, value.value))
        else:
            self.result.unresolved.append(
                Unresolved(
                    self.path, call.lineno, "Python -c source is not a literal or single binding"
                )
            )

    def finish(self) -> Inputs:
        for function, call in self._template_launches.values():
            if function not in self._used_functions:
                self.result.unresolved.append(
                    Unresolved(
                        self.path,
                        call.lineno,
                        "Python -c helper input has no resolved local caller",
                    )
                )
        return self.result


def inputs(tree: ast.AST, path: str) -> Inputs:
    """Resolve only actual Python -c inputs and transparent local helper calls."""
    visitor = _Inputs(tree, path)
    visitor.visit(tree)
    return visitor.finish()


@dataclass
class ImportFacts:
    """Imports and literal dynamic targets from one executed source string."""

    clauses: list[ast.Import | ast.ImportFrom] = field(
        default_factory=list[ast.Import | ast.ImportFrom]
    )
    targets: list[str] = field(default_factory=list[str])
    unresolved: list[Unresolved] = field(default_factory=list[Unresolved])


def import_facts(source: Source, path: str) -> ImportFacts:
    """Parse Python source, retaining unknown dynamic targets and invalid inputs."""
    facts = ImportFacts()
    try:
        tree = ast.parse(source.text)
    except SyntaxError as error:
        facts.unresolved.append(
            Unresolved(path, source.line, f"Python -c source is invalid: {error.msg}")
        )
        return facts
    visitor = _ImportFacts(tree, source, path, facts)
    visitor.visit(tree)
    return facts


class _ImportFacts(ast.NodeVisitor):
    def __init__(self, tree: ast.AST, source: Source, path: str, facts: ImportFacts) -> None:
        self.scope = _Scope(tree, "")
        self.source, self.path, self.facts = source, path, facts

    def _nested(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        parent = self.scope
        self.scope = _Scope(node, "", parent.nested_parent())
        self.generic_visit(node)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        parent = self.scope
        self.scope = _Scope(node, "", parent.nested_parent())
        self.generic_visit(node)
        self.scope = parent

    def visit_Import(self, node: ast.Import) -> None:
        self.facts.clauses.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.facts.unresolved.append(
                Unresolved(
                    self.path, self.source.line, "Python -c source has no relative import package"
                )
            )
        else:
            self.facts.clauses.append(node)

    def visit_Call(self, node: ast.Call) -> None:
        origin = self.scope.origin(node.func)
        if origin in _DYNAMIC_IMPORTS:
            first = (
                node.args[0]
                if node.args
                else next(
                    (kw.value for kw in node.keywords if kw.arg in {"name", "mod_name"}), None
                )
            )
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and not first.value.startswith(".")
            ):
                self.facts.targets.append(first.value)
            else:
                self.facts.unresolved.append(
                    Unresolved(
                        self.path,
                        self.source.line,
                        f"{origin} target is not a literal absolute module",
                    )
                )
        self.generic_visit(node)
