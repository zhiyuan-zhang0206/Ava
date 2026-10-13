"""The module index and reference collector behind the tests-location proof and test selection.

A test file's first-party references are collected from import statements (also inside functions),
string targets of `importlib.import_module` / `importorskip` and friends, paths below the
repository root naming a source file (`ROOT / "scripts" / "lint" / "x.py"`) and imports in source
strings the file runs. Evidence that exists only because the file patches something, or only
because it carries sample data, does not count: a patched module is not the subject, and a path
counts only as a chain from the repository root. `imports/executed.py` retains unknown inputs and
`placement_evidence.py` owns root paths.

What this module no longer does is decide a home or a layer: placing a test by the former import-linter
contracts, and judging private patches by that placement, were replaced by meizel
(`meizel/docs/checker/checker.ava.okf.md`).
"""

from __future__ import annotations

import ast
import collections
import functools
import re
from collections.abc import Sequence
from pathlib import Path

from meizel.roles import packages
from scripts.structure import placement_evidence, service_units
from scripts.structure.imports import cache, executed, facts
from scripts.structure.placement_evidence import (
    IncompleteReferenceEvidenceError,
    Ref,
    ReferenceEvidence,
)

__all__ = [
    "CODE_TOPS",
    "ModuleIndex",
    "collect_reference_evidence",
    "collect_references",
    "common_dir",
    "unit_of",
    "without_patch_evidence",
]

# First-party Python code participates in placement, including runnable templates.
CODE_TOPS = packages("application", "tooling", "templates", "examples")
_ARTIFACT_TOPS = (
    "db",
    "deploy",
    "ui",
    "migrations",
    ".github",
    "docs",
    "future",
    ".agents",
    "assets",
)
_ROOT_FILES = (
    "pyproject.toml",
    "uv.lock",
    ".pre-commit-config.yaml",
    "AGENTS.md",
    ".gitleaks.toml",
    "CHANGELOG.md",
)
_PATH_ROOTS = (*CODE_TOPS, *_ARTIFACT_TOPS, *_ROOT_FILES)


# The subset of string-target callees whose target is *replaced*, not imported.
_PATCH_CALLEES = frozenset({"setattr", "delattr", "patch", "dict", "multiple"})
# `<receiver>.setattr(...)` is a monkeypatch call unless the receiver is one of these.
_NOT_MONKEYPATCH = frozenset({"self", "cls", "os", "sys", "object", "builtins", "super"})
_MONKEYPATCH_TARGET_CALLS = frozenset({"setattr", "delattr", "setitem", "delitem"})
_DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def unit_of(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] not in CODE_TOPS:
        return None
    if parts[0] == "services" and len(parts) > 1:
        return service_units.unit_of(parts)
    return parts[0]


# --------------------------------------------------------------------------- module index


class ModuleIndex:
    """Resolves dotted names and path literals against a checkout."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._kinds: dict[str, str | None] = {}
        self._prefixes: dict[str, str | None] = {}

    def kind(self, dotted: str) -> str | None:
        """'file' | 'pkg' | 'ns' | None for a dotted module path."""
        if dotted in self._kinds:
            return self._kinds[dotted]
        path = self.repo_root.joinpath(*dotted.split("."))
        found: str | None = None
        if path.with_suffix(".py").is_file():
            found = "file"
        elif (path / "__init__.py").is_file():
            found = "pkg"
        elif path.is_dir() and any(path.rglob("*.py")):
            found = "ns"
        self._kinds[dotted] = found
        return found

    def file(self, dotted: str) -> str | None:
        """Exact module source or package door; namespace packages have no file."""
        kind = self.kind(dotted)
        if kind == "file":
            return dotted.replace(".", "/") + ".py"
        if kind == "pkg":
            return dotted.replace(".", "/") + "/__init__.py"
        return None

    def resolve_prefix(self, dotted: str, tops: Sequence[str] = CODE_TOPS) -> str | None:
        """Longest dotted prefix that is a module; None if the top segment is not code."""
        parts = dotted.split(".")
        if parts[0] not in tops:
            return None
        if dotted not in self._prefixes:
            self._prefixes[dotted] = next(
                (
                    candidate
                    for length in range(len(parts), 0, -1)
                    if self.kind(candidate := ".".join(parts[:length]))
                ),
                None,
            )
        return self._prefixes[dotted]

    def split(self, dotted: str) -> tuple[str, list[str]] | None:
        """(longest first-party module prefix, remaining attribute segments), or None."""
        module = self.resolve_prefix(dotted, CODE_TOPS)
        if module is None:
            return None
        return module, dotted[len(module) :].split(".")[1:]

    @functools.cached_property
    def importers(self) -> dict[str, set[str]]:
        """Module prefix -> the directories whose non-test subtree directly imports it.

        `a.b.c` imported from `x/y/z.py` files `a`, `a.b` and `a.b.c` under `x`, `x/y`. Direct
        edges only, function-level imports included; resolved against this checkout.
        """
        found: dict[str, set[str]] = collections.defaultdict(set)
        for rel, statements in cache.production_imports(self.repo_root, CODE_TOPS).items():
            parts = rel.split("/")[:-1]
            directories = {"/".join(parts[:depth]) for depth in range(1, len(parts) + 1)}
            for ref in collect_references(ast.parse(statements), self):
                if ref.kind == "import":
                    names = ref.module.split(".")
                    for depth in range(1, len(names) + 1):
                        found[".".join(names[:depth])] |= directories
        return found

    def dir_of(self, module: str) -> str:
        """Directory owning the module: its parent dir for a file, itself for a package."""
        parts = module.split(".")
        return "/".join(parts[:-1]) if self.kind(module) == "file" else "/".join(parts)

    def path_to_module(self, rel: str) -> str | None:
        """Repo-relative path literal -> nearest enclosing dotted module (code dirs only)."""
        parts = rel.strip("/").split("/")
        if parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        if parts[0] not in CODE_TOPS:
            return None
        while parts:
            candidate = ".".join(parts)
            if all(re.fullmatch(r"[A-Za-z_]\w*", part) for part in parts) and self.kind(candidate):
                return candidate
            parts.pop()
        return None


# --------------------------------------------------------------------------- unit graph


# --------------------------------------------------------------------------- references


def _callee(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _const_strings(args: Sequence[ast.expr]) -> list[str] | None:
    """The literal string arguments, or None unless every argument is one."""
    if args and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
        return [str(a.value) for a in args if isinstance(a, ast.Constant)]
    return None


def _div_operands(node: ast.AST) -> list[str | None]:
    """Left-to-right operands of a `/` chain; literals as str, everything else None."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_operands(node.left) + _div_operands(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


class _Collector(ast.NodeVisitor):
    def __init__(self, index: ModuleIndex, tree: ast.AST, rel_path: str) -> None:
        self.index = index
        self.refs: list[Ref] = []
        self._handled: set[int] = set()
        self._div_seen: set[int] = set()
        self._roots = placement_evidence.RepoRoots(tree, rel_path)
        self._rel_path = rel_path

    def _add(
        self, line: int, kind: str, module: str, via: str = "", names: tuple[str, ...] = ()
    ) -> None:
        unit = unit_of(module) if "tests" not in module.split(".") else None
        if unit:
            self.refs.append(Ref(line, kind, module, unit, via, names))

    def _add_dotted(
        self, line: int, kind: str, dotted: str, via: str = "", names: tuple[str, ...] = ()
    ) -> None:
        module = self.index.resolve_prefix(dotted)
        if module:
            self._add(line, kind, module, via, names)

    def visit_Import(self, node: ast.Import) -> None:
        pass  # Runtime clauses are collected by the shared facts owner.

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        pass

    def visit_Call(self, node: ast.Call) -> None:
        callee = _callee(node.func)
        last = callee.rsplit(".", 1)[-1]
        constants = _const_strings(node.args)
        if last == "load_skill_script" and constants:
            self._note_path(node.lineno, "ava_builtins/skills/" + "/".join(constants))
        if (
            last == "joinpath"
            and constants
            and constants[0] in _PATH_ROOTS
            and isinstance(node.func, ast.Attribute)
            and self._roots.is_root(node.func.value)
        ):
            self._note_path(node.lineno, "/".join(c.strip("/") for c in constants))
        if last in _PATCH_CALLEES and node.args:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _DOTTED.match(first.value)
            ):
                self._add_dotted(node.lineno, "string-target", first.value, via=callee)
                self._handled.add(id(first))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and id(node) not in self._div_seen:
            inner: ast.expr = node
            while isinstance(inner, ast.BinOp):
                self._div_seen.add(id(inner))
                inner = inner.left
            chunk: list[str] = []
            for operand in _div_operands(node)[1:]:  # `inner` is the leftmost operand
                if operand is None:
                    break
                chunk.append(operand.strip("/"))
            if chunk and chunk[0].split("/")[0] in _PATH_ROOTS and self._roots.is_root(inner):
                self._note_path(node.lineno, "/".join(chunk))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not isinstance(value, str):
            return
        if len(value) > 200 or "\n" in value or " " in value or id(node) in self._handled:
            return
        if _DOTTED.match(value) and value.split(".")[0] in CODE_TOPS:
            module = self.index.resolve_prefix(value)
            if module and module.count(".") >= 1:
                self._add(node.lineno, "string-loose", module)
                return

    def _note_path(self, line: int, rel: str) -> None:
        if rel.split("/", maxsplit=1)[0] not in CODE_TOPS:
            return
        module = self.index.path_to_module(rel)
        if module:
            is_file = rel.endswith(".py") and (self.index.repo_root / rel).is_file()
            self._add(line, "path-file" if is_file else "path-dir", module)


def collect_reference_evidence(
    tree: ast.AST, index: ModuleIndex, rel_path: str = ""
) -> ReferenceEvidence:
    """Actual source references and bounded Python -c evidence, without losing gaps."""
    collector = _Collector(index, tree, rel_path)
    collector.visit(tree)
    evidence = facts.collect(tree, rel_path, index, tops=CODE_TOPS)
    for fact in evidence.records:
        if fact.kind == facts.FactKind.RESOURCE:
            continue  # Resource ownership is independent of the legacy module-home query.
        kind = "import" if fact.kind == facts.FactKind.IMPORT else "embedded-import"
        if fact.kind == facts.FactKind.DYNAMIC_IMPORT:
            kind = "string-target"
        collector._add(fact.line, kind, fact.target, via=fact.via, names=fact.names)
    unresolved = [
        executed.Unresolved(u.path, u.line, u.reason)
        for u in evidence.unknown
        if u.kind != facts.FactKind.RESOURCE
    ]
    return ReferenceEvidence(collector.refs, unresolved)


def collect_references(tree: ast.AST, index: ModuleIndex, rel_path: str = "") -> list[Ref]:
    """Complete references only; callers needing incomplete facts use the evidence API.

    `rel_path` anchors actual relative imports. The API does not guess a package
    anchor for synthetic input or silently return a partial dependency list.
    """
    evidence = collect_reference_evidence(tree, index, rel_path)
    if evidence.unresolved:
        raise IncompleteReferenceEvidenceError(evidence)
    return evidence.refs


def _patch_target_root(call: ast.Call) -> ast.Name | None:
    """The Name at the root of a patch call's object argument (`mod`, `mod.sub.attr`)."""
    name = _callee(call.func)
    last = name.rsplit(".", 1)[-1]
    receiver = name.rsplit(".", 1)[0] if "." in name else ""
    monkeypatch = (
        bool(receiver)
        and receiver.split(".")[0] not in _NOT_MONKEYPATCH
        and last in _MONKEYPATCH_TARGET_CALLS
    )
    mock = name.endswith((".patch.object", ".patch.dict", ".patch.multiple")) or name in {
        "patch.object",
        "patch.dict",
        "patch.multiple",
    }
    if not (monkeypatch or mock) or not call.args:
        return None
    node = call.args[0]
    while isinstance(node, ast.Attribute):
        node = node.value
    return node if isinstance(node, ast.Name) else None


def _patch_only_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Names read only as the object a patch call replaces something on."""
    roots = {
        id(root)
        for node in nodes
        if isinstance(node, ast.Call) and (root := _patch_target_root(node)) is not None
    }
    patched: collections.Counter[str] = collections.Counter()
    other: collections.Counter[str] = collections.Counter()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            (patched if id(node) in roots else other)[node.id] += 1
    return {name for name in patched if other[name] == 0}


def without_patch_evidence(nodes: Sequence[ast.AST], refs: list[Ref]) -> list[Ref]:
    """`refs` minus what exists only because the file patches it (see the module docstring)."""
    patch_only = _patch_only_names(nodes)
    return [
        ref
        for ref in refs
        if not (ref.kind == "string-target" and ref.via.rsplit(".", 1)[-1] in _PATCH_CALLEES)
        and not (ref.kind == "import" and ref.names and all(n in patch_only for n in ref.names))
    ]


# --------------------------------------------------------------------------- placement


def common_dir(dirs: list[str]) -> str:
    """The nearest common ancestor directory of `dirs`."""
    common: list[str] = []
    for parts in zip(*(d.split("/") for d in dirs), strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common)
