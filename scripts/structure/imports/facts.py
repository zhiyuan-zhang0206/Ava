"""Direct runtime dependencies and explicit gaps, without placement policy."""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from scripts.structure import placement_evidence

from . import (
    ModuleSourceLookup,
    bindings,
    dependency_evidence,
    executed,
    file_loader,
    mock_targets,
    normalize,
)

__all__ = ["Evidence", "Fact", "FactKind", "FileExecution", "ResourceRead", "Unknown", "collect"]


class FactKind(StrEnum):
    IMPORT = "import"
    DYNAMIC_IMPORT = "dynamic-import"
    EMBEDDED_IMPORT = "embedded-import"
    PYTHON_MODULE = "python-module"
    RESOURCE = "resource"


@dataclass(frozen=True)
class Fact:
    line: int
    kind: FactKind
    target: str
    names: tuple[str, ...] = ()
    via: str = ""  # recognized callee, for consumers' separate ownership policies


@dataclass(frozen=True)
class Unknown:
    path: str
    line: int
    expression: str
    reason: str
    kind: FactKind = FactKind.DYNAMIC_IMPORT


@dataclass(frozen=True)
class ResourceRead:
    """A recognized read operation on a checkout path, without ownership policy."""

    line: int
    target: str
    operation: str


@dataclass(frozen=True)
class FileExecution:
    """A possible input to a proven exec call, with its runtime module identity."""

    line: int
    target: str
    name: str


@dataclass(frozen=True)
class Evidence:
    records: tuple[Fact, ...]
    unknown: tuple[Unknown, ...]
    resource_reads: tuple[ResourceRead, ...] = ()
    file_executions: tuple[FileExecution, ...] = ()


_DYNAMIC_CALLS = frozenset(
    {
        "importlib.import_module",
        "importlib.util.find_spec",
        "runpy.run_module",
        "__import__",
        "pytest.importorskip",
    }
)
_PATCH_IMPORTS = frozenset(
    {"unittest.mock.patch", "unittest.mock.patch.multiple", "unittest.mock.patch.dict"}
)


class _Collector(ast.NodeVisitor):
    def __init__(
        self,
        tree: ast.AST,
        path: str,
        index: ModuleSourceLookup,
        tops: Sequence[str],
        *,
        embedded: bool = False,
        loaded_name: str | None = None,
    ) -> None:
        self.path, self.index, self.tops = path, index, tops
        name = "__main__" if embedded else bindings.module_name(path)
        if loaded_name is not None:
            name = loaded_name
        self.context = bindings.module_context(tree, name)
        self.scope = self.context.scope(tree, path)
        self.scope_trees = {self.scope: tree}
        self.depth = len(Path(path).parts) if path else 0
        self.embedded = embedded
        self.loaded_name = loaded_name
        self.resource_seen: set[int] = set()
        self.has_launches = False
        self.visitors: dict[type[ast.AST], Callable[[ast.NodeVisitor, ast.AST], None]] = {}
        self.records: list[Fact] = []
        self.unknown: list[Unknown] = []
        self.resource_reads: list[ResourceRead] = []
        self.file_executions: list[FileExecution] = []
        self.file_specs: dict[ast.Call, tuple[bindings.Scope, Unknown]] = {}
        self.loaded_trees: dict[str, ast.Module] = {}
        self.seen_calls: set[ast.Call] = set()

    def visit(self, node: ast.AST) -> None:
        """Resolve visitor dispatch once per node type in this source analysis."""
        node_type = type(node)
        visitor = self.visitors.get(node_type)
        if visitor is None:
            visitor = getattr(
                type(self), "visit_" + node_type.__name__, ast.NodeVisitor.generic_visit
            )
            self.visitors[node_type] = visitor
        visitor(self, node)

    def visit_Name(self, node: ast.Name) -> None:
        """Names are resolved by their enclosing operation and completed Scope."""

    def visit_Constant(self, node: ast.Constant) -> None:
        """Literal payloads are resolved at their read, import or execution site."""

    def gap(
        self, node: ast.expr | ast.stmt, reason: str, kind: FactKind = FactKind.DYNAMIC_IMPORT
    ) -> None:
        self.unknown.append(Unknown(self.path, node.lineno, ast.unparse(node), reason, kind))

    def module(self, node: ast.expr | ast.stmt, target: str, kind: FactKind) -> None:
        if target.split(".", maxsplit=1)[0] not in self.tops:
            return
        if self.index.kind(target) is None:
            self.gap(node, f"First-party module does not exist: {target}", kind)
        else:
            self.records.append(Fact(node.lineno, kind, target))

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        if self.embedded and isinstance(node, ast.ImportFrom) and node.level:
            self.gap(
                node, "Python -c source has no relative import package", FactKind.EMBEDDED_IMPORT
            )
            return
        if self.loaded_name is not None and isinstance(node, ast.ImportFrom) and node.level:
            self.gap(node, "File-loader relative import package is not proven", FactKind.IMPORT)
            return
        clause = normalize(node, self.path)
        evidence = dependency_evidence(clause, self.index, self.tops)
        self.records.extend(
            Fact(node.lineno, FactKind.IMPORT, dep.module, dep.names) for dep in evidence.resolved
        )
        for target in evidence.unknown:
            self.gap(node, f"First-party module does not exist: {target}", FactKind.IMPORT)

    def visit_Import(self, node: ast.Import) -> None:
        self._import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._import(node)

    def _nested(self, node: bindings.ScopeNode) -> None:
        parent = self.scope
        outer, inner = bindings.scope_parts(node)
        for expression in outer:
            self.visit(expression)
        self.scope = self.context.scope(node, self.path, parent.nested_parent())
        self.scope_trees[self.scope] = node
        for statement in inner:
            self.visit(statement)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._nested(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._nested(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._nested(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._nested(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._nested(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.seen_calls.add(node)
        origin = self.scope.origin(node.func)
        if origin in _DYNAMIC_CALLS:
            self._dynamic(node, origin)
        if origin == "importlib.util.spec_from_file_location":
            self.gap(node, "File-loader spec has no proven execution chain")
            self.file_specs[node] = (self.scope, self.unknown[-1])
        if origin == "runpy.run_path":
            self.gap(node, f"File-loader {origin} execution inputs are not supported")
        self._file_execute(node)
        if origin in _PATCH_IMPORTS:
            self._patch_import(node, origin)
        if executed.is_launcher(origin):
            self.has_launches = True
            target, reason = executed.module_input(node, self.scope)
            if target is not None:
                values = self.scope.strings(target)
                if values is None:
                    self.gap(
                        node, "Python -m target is not bounded literal text", FactKind.PYTHON_MODULE
                    )
                else:
                    for value in values:
                        self.module(node, value, FactKind.PYTHON_MODULE)
            elif reason is not None:
                self.gap(node, reason, FactKind.PYTHON_MODULE)
        self._resource(node)
        if not self.embedded:
            self._resource_read(node)
        self.generic_visit(node)

    def _file_execute(self, node: ast.Call) -> None:
        proof = file_loader.prove_execution(
            node, self.scope, self.scope_trees[self.scope], self.seen_calls
        )
        if proof is None:
            return
        if proof.reason or proof.spec is None or proof.source is None:
            self.gap(node, proof.reason or "File-loader execution has no proven source")
            return
        found = self.file_specs.get(proof.spec)
        if (
            self.embedded
            or self.loaded_name is not None
            or found is None
            or found[0] is not self.scope
        ):
            self.gap(node, "File-loader execution is not a supported local source chain")
            return
        inputs = self._file_inputs(proof.spec, proof.source)
        if not inputs:
            self.gap(node, "File-loader name and path have no bounded unchanged checkout anchor")
            return
        changed = self._source_change(node, inputs)
        if changed:
            self.gap(*changed, kind=FactKind.RESOURCE)
            return
        for name, path in inputs:
            self._file_source(node, name, path)
        if found[1] in self.unknown:
            self.unknown.remove(found[1])

    def _file_inputs(self, spec: ast.Call, source: ast.expr) -> tuple[tuple[str, str], ...] | None:
        inputs = file_loader.input_domain(spec, self.scope, self._file_path_text(source))
        if not inputs or any(
            not self._unmodified_path_operation(spec, path, "", function=False)
            for _, path in inputs
        ):
            return None
        return inputs

    def _source_change(
        self, execution: ast.Call, inputs: tuple[tuple[str, str], ...]
    ) -> tuple[ast.Call, str] | None:
        for call, scope in file_loader.prior_calls(execution, self.scope):
            reason = self._source_write(call, scope, inputs)
            if reason:
                return call, reason
        return None

    def _source_write(
        self, node: ast.Call, scope: bindings.Scope, inputs: tuple[tuple[str, str], ...]
    ) -> str | None:
        previous, self.scope = self.scope, scope
        try:
            target = self._write_target(node)
            if target is None:
                return None
            anchored = self._file_path_text(target)
            if anchored is None:
                target = self._write_argument(target)
            values = scope.strings(anchored if anchored is not None else target)
            if not values or (anchored is None and any(not Path(v).is_absolute() for v in values)):
                return "File-loader prior write has no proven checkout or external target"
            outputs = {(self.index.repo_root / value).resolve() for value in values}
            sources = {(self.index.repo_root / path).resolve() for _, path in inputs}
            if outputs & sources:
                return "File-loader source has a prior recognized write"
        except (OSError, ValueError, RuntimeError) as error:
            return f"File-loader prior write target cannot be resolved: {error}"
        finally:
            self.scope = previous
        return None

    def _write_argument(self, target: ast.expr) -> ast.expr:
        value = self.scope.value(target)
        if (
            isinstance(value, ast.Call)
            and len(value.args) == 1
            and not value.keywords
            and self.scope.unmodified_origin(value.func) == "pathlib.Path"
        ):
            return value.args[0]
        return value

    def _write_target(self, node: ast.Call) -> ast.expr | None:
        method = node.func
        if isinstance(method, ast.Attribute) and method.attr in {"write_text", "write_bytes"}:
            return self.scope.value(method.value)
        function = self._open_function(method)
        if not function and not (isinstance(method, ast.Attribute) and method.attr == "open"):
            return None
        target = self._read_target(node)
        if target is None:
            return node
        if not self._unmodified_path_operation(node, "", "open", function=function):
            return node
        mode = self._open_mode(node)
        values = self.scope.strings(mode) if mode is not None else ("r",)
        if (
            self._readable_open(node)
            and values
            and not any(set(value) & set("wax+") for value in values)
        ):
            return None
        return target

    def _file_path_text(self, node: ast.expr) -> ast.expr | None:
        value = node
        path = self._resource_path(value)
        if path is not None:
            return ast.Constant(path)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
            prefix = self._resource_path(value.left)
            if prefix is not None:
                return ast.JoinedStr(
                    [
                        ast.Constant(prefix + "/" if prefix else ""),
                        ast.FormattedValue(value.right, -1),
                    ]
                )
        return None

    def _file_source(self, node: ast.Call, name: str, path: str) -> None:
        source = self.index.repo_root / path
        try:
            physical = source.resolve()
            if (
                source.suffix != ".py"
                or physical != source.absolute()
                or not physical.is_relative_to(self.index.repo_root.resolve())
            ):
                self.gap(node, "File-loader source is not a checkout Python file")
                return
            if path not in self.loaded_trees:
                self.loaded_trees[path] = ast.parse(
                    source.read_text(encoding="utf-8"), filename=path
                )
        except (OSError, UnicodeError, SyntaxError, ValueError) as error:
            self.gap(node, f"File-loader source {path} cannot be analyzed: {error}")
            return
        child = _Collector(self.loaded_trees[path], path, self.index, self.tops, loaded_name=name)
        evidence = _analyze(self.loaded_trees[path], child)
        self.records.append(Fact(node.lineno, FactKind.RESOURCE, path, via="file-loader"))
        self.file_executions.append(FileExecution(node.lineno, path, name))
        for fact in evidence.records:
            for target in self._file_dependencies(fact):
                self.records.append(Fact(node.lineno, FactKind.RESOURCE, target, via="file-loader"))
        self.unknown.extend(
            Unknown(
                self.path,
                node.lineno,
                gap.expression,
                f"File-loader {path}:{gap.line}: {gap.reason}",
                gap.kind,
            )
            for gap in evidence.unknown
        )

    def _file_dependencies(self, fact: Fact) -> tuple[str, ...]:
        if fact.kind is FactKind.RESOURCE:
            return (fact.target,)
        parts = fact.target.split(".")
        files = tuple(
            file
            for end in range(1, len(parts) + 1)
            if (file := self.index.file(".".join(parts[:end]))) is not None
        )
        return files or (fact.target.replace(".", "/"),)

    def _dynamic(self, node: ast.Call, origin: str) -> None:
        first = (
            node.args[0]
            if node.args
            else next((kw.value for kw in node.keywords if kw.arg in {"name", "mod_name"}), None)
        )
        values = self.scope.strings(first) if first is not None else None
        if values is None:
            self.gap(node, f"{origin} target is not bounded literal text")
            return
        for target in values:
            resolved = self._relative_target(node, target) if target.startswith(".") else target
            if resolved:
                self.module(node, resolved, FactKind.DYNAMIC_IMPORT)

    def _patch_import(self, node: ast.Call, origin: str) -> None:
        keyword_name = "in_dict" if origin == "unittest.mock.patch.dict" else "target"
        target = (
            node.args[0]
            if node.args
            else next(
                (keyword.value for keyword in node.keywords if keyword.arg == keyword_name), None
            )
        )
        values = self.scope.strings(target) if target is not None else None
        if values is None:
            if (
                target is not None
                and origin != "unittest.mock.patch"
                and mock_targets.is_object(target, self.scope, self.index)
            ):
                return  # An actual object target does not invoke patch's string importer.
            self.gap(node, f"{origin} target is not bounded literal text")
            return
        for value in values:
            self._patch_module(node, origin, value)

    def _patch_module(self, node: ast.Call, origin: str, value: str) -> None:
        candidate = value.rsplit(".", maxsplit=1)[0] if origin == "unittest.mock.patch" else value
        if candidate.split(".", maxsplit=1)[0] not in self.tops:
            return
        module = self.index.resolve_prefix(candidate, self.tops)
        if module is None:
            self.gap(node, f"First-party patch target has no module: {value}")
            return
        self.records.append(Fact(node.lineno, FactKind.DYNAMIC_IMPORT, module, via=origin))
        if module != candidate and self.index.kind(module) != "file":
            self.gap(node, f"Patch target traverses an unverified package attribute: {value}")

    def _relative_target(self, node: ast.Call, target: str) -> str:
        package = (
            node.args[1]
            if len(node.args) > 1
            else next((kw.value for kw in node.keywords if kw.arg == "package"), None)
        )
        values = self.scope.strings(package) if package is not None else None
        if values is None or len(values) != 1:
            self.gap(node, "Relative dynamic import has no literal package anchor")
            return ""
        try:
            return importlib.util.resolve_name(target, values[0])
        except ImportError as error:
            self.gap(node, str(error))
            return ""

    def _resource_path(self, node: ast.AST, seen: frozenset[str] = frozenset()) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            value = self.scope.value(node)
            return self._resource_path(value, seen | {node.id}) if value is not node else None
        file_path = self._file_path(node)
        if file_path is not None:
            return file_path
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            prefix = self._resource_path(node.value, seen)
            if prefix:
                parent = Path(prefix).parent.as_posix()
                return "" if parent == "." else parent
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._divided_path(node, seen)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return self._method_path(node, node.func, seen)
        return None

    def _method_path(
        self, node: ast.Call, method: ast.Attribute, seen: frozenset[str]
    ) -> str | None:
        if method.attr == "joinpath":
            return self._joined_path(method.value, node.args, seen)
        if (
            method.attr in {"with_name", "with_suffix"}
            and len(node.args) == 1
            and not node.keywords
        ):
            return self._renamed_path(method.value, method.attr, node.args[0], seen)
        return None

    def _renamed_path(
        self, base: ast.expr, method: str, argument: ast.expr, seen: frozenset[str]
    ) -> str | None:
        prefix = self._resource_path(base, seen)
        values = self.scope.strings(argument)
        if not prefix or values is None or len(values) != 1:
            return None
        path = Path(prefix)
        renamed = (
            path.with_name(values[0]) if method == "with_name" else path.with_suffix(values[0])
        )
        return renamed.as_posix()

    def _file_path(self, node: ast.AST) -> str | None:
        ascents = placement_evidence.file_ascents(
            node, lambda expr: self.scope.origin(expr) == "pathlib.Path"
        )
        if (
            not self.depth
            or self.scope.bound("__file__")
            or ascents is None
            or ascents > self.depth
        ):
            return None
        path = Path(self.path)
        for _ in range(ascents):
            path = path.parent
        return "" if path.as_posix() == "." else path.as_posix()

    def _divided_path(self, node: ast.BinOp, seen: frozenset[str]) -> str | None:
        prefix = self._resource_path(node.left, seen)
        suffix = self.scope.strings(node.right)
        if prefix is not None and suffix is not None and len(suffix) == 1:
            return str(Path(prefix) / suffix[0])
        return None

    def _joined_path(
        self, base: ast.expr, args: list[ast.expr], seen: frozenset[str]
    ) -> str | None:
        prefix = self._resource_path(base, seen)
        parts = [self.scope.strings(arg) for arg in args]
        if prefix is not None and parts and all(p is not None and len(p) == 1 for p in parts):
            return str(Path(prefix).joinpath(*(p[0] for p in parts if p is not None)))
        return None

    def _resource(self, node: ast.expr) -> None:
        if self.embedded:
            return  # Python -c has no source-file __file__ anchor.
        if id(node) in self.resource_seen:
            return
        if self._file_path(node) is not None:
            return  # A file/root anchor alone is a builder, not a resource reference.
        target = self._resource_path(node)
        if not target or target == ".":
            return
        self.resource_seen.update(id(child) for child in ast.walk(node))
        path = Path(target)
        if path.is_absolute() or ".." in path.parts:
            self.gap(node, "Repository resource escapes its root", FactKind.RESOURCE)
            return
        self.records.append(Fact(node.lineno, FactKind.RESOURCE, path.as_posix()))

    def _read_target(self, node: ast.Call) -> ast.expr | None:
        if self._open_function(node.func):
            return (
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "file"), None)
            )
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "open",
            "read_text",
            "read_bytes",
            "iterdir",
            "glob",
            "rglob",
        }:
            return None
        receiver = self.scope.value(node.func.value)
        file_read = node.func.attr in {"read_text", "read_bytes"}
        if self._resource_path(receiver) is not None:
            return receiver
        if isinstance(receiver, ast.Call) and self.scope.origin(receiver.func) == "pathlib.Path":
            if receiver.args:
                return receiver.args[0]
            return receiver if file_read else None
        return receiver if file_read or self._path_expression(receiver) else None

    def _open_function(self, node: ast.expr) -> bool:
        builtin = isinstance(node, ast.Name) and node.id == "open" and not self.scope.bound("open")
        return builtin or self.scope.origin(node) in {"builtins.open", "io.open"}

    def _path_expression(self, node: ast.expr) -> bool:
        if self._resource_path(node) is not None:
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._path_expression(node.left)
        if isinstance(node, ast.Attribute) and node.attr in {"parent", "parents"}:
            return self._path_expression(node.value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            return self._path_expression(node.value)
        return self._path_call(node)

    def _path_call(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Call):
            if self.scope.origin(node.func) == "pathlib.Path":
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "resolve",
                "absolute",
                "joinpath",
                "with_name",
                "with_suffix",
            }:
                return self._path_expression(node.func.value)
        return False

    def _write_only(self, node: ast.Call) -> bool:
        """An ``open`` whose literal mode cannot read: its file is an output, not an input."""
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes"}:
            return False
        mode = self._open_mode(node)
        values = self.scope.strings(mode) if mode is not None else None
        return bool(values) and all(
            "r" not in value and "+" not in value and any(flag in value for flag in "wax")
            for value in values or ()
        )

    def _open_mode(self, node: ast.Call) -> ast.expr | None:
        keyword = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
        if keyword is not None:
            return keyword
        if self._open_function(node.func):
            return node.args[1] if len(node.args) > 1 else None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "open":
            return node.args[0] if node.args else None
        return None

    def _resource_read(self, node: ast.Call) -> None:
        target = self._read_target(node)
        if target is None or self._write_only(node):
            return
        path = self._resource_path(target)
        if path is not None:
            self.records.append(Fact(node.lineno, FactKind.RESOURCE, path or "."))
            self._read_operation(node, path or ".")
            return
        values = self.scope.strings(target)
        if values and all(Path(value).is_absolute() for value in values):
            for value in values:
                absolute = Path(value)
                if absolute.is_relative_to(self.index.repo_root):
                    relative = absolute.relative_to(self.index.repo_root).as_posix()
                    self.records.append(Fact(node.lineno, FactKind.RESOURCE, relative))
                    self._read_operation(node, relative)
            return
        if self.scope.unmodified_origin(target) == "os.devnull":
            return
        self.gap(
            node,
            "Resource read has no proven repository or external path anchor",
            FactKind.RESOURCE,
        )

    def _read_operation(self, node: ast.Call, path: str) -> None:
        if Path(path).is_absolute() or ".." in Path(path).parts:
            return
        function = self._open_function(node.func)
        operation = "open" if function else getattr(node.func, "attr", "")
        if operation not in {"open", "read_text", "read_bytes"}:
            return  # Directory iterators require a separate consumption proof.
        if operation == "open" and not self._readable_open(node):
            return
        if self._unmodified_path_operation(node, path, operation, function=function):
            self.resource_reads.append(ResourceRead(node.lineno, path, operation))

    def _readable_open(self, node: ast.Call) -> bool:
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            kw.arg is None for kw in node.keywords
        ):
            return False
        mode = self._open_mode(node)
        values = self.scope.strings(mode) if mode is not None else ("r",)
        return bool(values) and all("r" in value or "+" in value for value in values or ())

    def _unmodified_path_operation(
        self, node: ast.Call, path: str, operation: str, *, function: bool
    ) -> bool:
        origin = self.scope.origin(node.func)
        if function and origin:
            return self.scope.unmodified_origin(node.func) == origin
        scope: bindings.Scope | None = self.scope
        while scope is not None:
            for written in scope.attribute_writes:
                origin = scope.origin(written)
                if function:
                    if origin == "builtins.open":
                        return False
                elif (
                    origin == "pathlib.Path"
                    or origin.startswith("pathlib.Path.")
                    or (written.attr == operation and self._resource_path(written.value) == path)
                ):
                    return False
            scope = scope.parent
        return True

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self._resource(node)
        self.generic_visit(node)


def collect(
    tree: ast.AST, rel_path: str, index: ModuleSourceLookup, *, tops: Sequence[str]
) -> Evidence:
    """Unpruned direct facts for a checkout, with every recognized unsupported input retained.

    Literal dynamic imports, finite pytest string domains, Python -m/-c launches
    and repository-root paths participate. This does not execute helpers, infer
    arbitrary builders or assign business ownership to fixture execution edges.
    """
    collector = _Collector(tree, rel_path, index, tops)
    return _analyze(tree, collector)


def _analyze(tree: ast.AST, collector: _Collector) -> Evidence:
    rel_path, index, tops = collector.path, collector.index, collector.tops
    collector.visit(tree)
    inputs = (
        executed.inputs(tree, rel_path, context=collector.context)
        if collector.has_launches
        else executed.Inputs()
    )
    collector.context.clear_scopes()
    collector.unknown.extend(
        Unknown(g.path, g.line, "Python -c", g.reason, FactKind.EMBEDDED_IMPORT)
        for g in inputs.unresolved
    )
    for source in inputs.sources:
        embedded = _embedded(source, rel_path, index, tops)
        collector.records.extend(embedded.records)
        collector.unknown.extend(embedded.unknown)
    return Evidence(
        tuple(dict.fromkeys(collector.records)),
        tuple(dict.fromkeys(collector.unknown)),
        tuple(dict.fromkeys(collector.resource_reads)),
        tuple(dict.fromkeys(collector.file_executions)),
    )


def _embedded(
    source: executed.Source, path: str, index: ModuleSourceLookup, tops: Sequence[str]
) -> Evidence:
    try:
        tree = ast.parse(source.text)
    except SyntaxError as error:
        return Evidence(
            (),
            (
                Unknown(
                    path,
                    source.line,
                    source.text,
                    f"Invalid Python -c source: {error.msg}",
                    FactKind.EMBEDDED_IMPORT,
                ),
            ),
        )
    collector = _Collector(tree, path, index, tops, embedded=True)
    collector.visit(tree)
    collector.context.clear_scopes()
    return Evidence(
        tuple(
            Fact(source.line, FactKind.EMBEDDED_IMPORT, fact.target, fact.names, fact.via)
            for fact in collector.records
        ),
        tuple(
            Unknown(path, source.line, gap.expression, gap.reason, FactKind.EMBEDDED_IMPORT)
            for gap in collector.unknown
        ),
    )
