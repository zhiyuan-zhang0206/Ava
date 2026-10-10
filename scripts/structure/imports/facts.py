"""Direct runtime dependencies and explicit gaps, without placement policy."""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from scripts.structure import placement_evidence

from . import ModuleLookup, bindings, dependency_evidence, executed, normalize


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


@dataclass(frozen=True)
class Unknown:
    path: str
    line: int
    expression: str
    reason: str
    kind: FactKind = FactKind.DYNAMIC_IMPORT


@dataclass(frozen=True)
class Evidence:
    records: tuple[Fact, ...]
    unknown: tuple[Unknown, ...]


class Lookup(ModuleLookup, Protocol):
    repo_root: Path


_DYNAMIC_CALLS = frozenset(
    {
        "importlib.import_module",
        "importlib.util.find_spec",
        "runpy.run_module",
        "__import__",
        "pytest.importorskip",
    }
)


class _Collector(ast.NodeVisitor):
    def __init__(
        self,
        tree: ast.AST,
        path: str,
        index: Lookup,
        tops: Sequence[str],
        *,
        embedded: bool = False,
    ) -> None:
        self.path, self.index, self.tops = path, index, tops
        self.scope = bindings.Scope(tree, path)
        self.depth = len(Path(path).parts) if path else 0
        self.embedded = embedded
        self.resource_seen: set[int] = set()
        self.records: list[Fact] = []
        self.unknown: list[Unknown] = []

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

    def _nested(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
    ) -> None:
        parent = self.scope
        self.scope = bindings.Scope(node, self.path, parent.nested_parent())
        self.generic_visit(node)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._nested(node)

    def visit_Call(self, node: ast.Call) -> None:
        origin = self.scope.origin(node.func)
        if origin in _DYNAMIC_CALLS:
            self._dynamic(node, origin)
        if origin in executed._LAUNCHERS:
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
        ascents = placement_evidence.file_ascents(
            node, lambda expr: self.scope.origin(expr) == "pathlib.Path"
        )
        if self.depth and not self.scope.bound("__file__") and ascents == self.depth:
            return ""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._divided_path(node, seen)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "joinpath"
        ):
            return self._joined_path(node.func.value, node.args, seen)
        return None

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
        builtin_open = (
            isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and not self.scope.bound("open")
        )
        if builtin_open or self.scope.origin(node.func) in {"builtins.open", "io.open"}:
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
        if isinstance(receiver, ast.Call) and self.scope.origin(receiver.func) == "pathlib.Path":
            return receiver.args[0] if receiver.args else None
        return receiver if self._path_expression(receiver) else None

    def _path_expression(self, node: ast.expr) -> bool:
        if self._resource_path(node) is not None:
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._path_expression(node.left)
        if isinstance(node, ast.Call):
            if self.scope.origin(node.func) == "pathlib.Path":
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "resolve",
                "absolute",
                "joinpath",
            }:
                return self._path_expression(node.func.value)
        return False

    def _resource_read(self, node: ast.Call) -> None:
        target = self._read_target(node)
        if target is None:
            return
        path = self._resource_path(target)
        if path is not None:
            if not path:
                self.records.append(Fact(node.lineno, FactKind.RESOURCE, "."))
            return
        values = self.scope.strings(target)
        if values and all(Path(value).is_absolute() for value in values):
            for value in values:
                absolute = Path(value)
                if absolute.is_relative_to(self.index.repo_root):
                    relative = absolute.relative_to(self.index.repo_root).as_posix()
                    self.records.append(Fact(node.lineno, FactKind.RESOURCE, relative))
            return
        self.gap(
            node,
            "Resource read has no proven repository or external path anchor",
            FactKind.RESOURCE,
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self._resource(node)
        self.generic_visit(node)


def collect(tree: ast.AST, rel_path: str, index: Lookup, *, tops: Sequence[str]) -> Evidence:
    """Unpruned direct facts for a checkout, with every recognized unsupported input retained.

    Literal dynamic imports, finite pytest string domains, Python -m/-c launches
    and repository-root paths participate. This does not execute helpers, infer
    arbitrary builders or assign business ownership to fixture execution edges.
    """
    collector = _Collector(tree, rel_path, index, tops)
    collector.visit(tree)
    inputs = executed.inputs(tree, rel_path)
    collector.unknown.extend(
        Unknown(g.path, g.line, "Python -c", g.reason, FactKind.EMBEDDED_IMPORT)
        for g in inputs.unresolved
    )
    for source in inputs.sources:
        embedded = _embedded(source, rel_path, index, tops)
        collector.records.extend(embedded.records)
        collector.unknown.extend(embedded.unknown)
    return Evidence(
        tuple(dict.fromkeys(collector.records)), tuple(dict.fromkeys(collector.unknown))
    )


def _embedded(source: executed.Source, path: str, index: Lookup, tops: Sequence[str]) -> Evidence:
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
    return Evidence(
        tuple(
            Fact(source.line, FactKind.EMBEDDED_IMPORT, fact.target, fact.names)
            for fact in collector.records
        ),
        tuple(
            Unknown(path, source.line, gap.expression, gap.reason, FactKind.EMBEDDED_IMPORT)
            for gap in collector.unknown
        ),
    )
