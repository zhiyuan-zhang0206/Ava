"""The names a first-party module binds at its top level, found without importing it."""

from __future__ import annotations

import ast
import builtins
import types
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

__all__ = ["Bindings", "module_bindings", "provides"]

# Names every module object has, and the builtins its code resolves when it binds no such name.
_ALWAYS = frozenset(dir(types.ModuleType("m"))) | frozenset(dir(builtins))
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True, slots=True)
class Bindings:
    """The top-level names of one module, and whether it can serve others at run time."""

    names: frozenset[str]
    dynamic: bool


def provides(bindings: Bindings, name: str) -> bool:
    """Whether setting or deleting `name` on the module acts on something that exists."""
    return bindings.dynamic or name in bindings.names or name in _ALWAYS


def module_bindings(root: Path, file: str) -> Bindings:
    """The bindings of `root/file`; an unreadable or unparsable file is treated as dynamic."""
    try:
        stat = (root / file).stat()
    except OSError:
        return Bindings(frozenset(), dynamic=True)
    return _read(str(root / file), stat.st_mtime_ns, stat.st_size)


@cache
def _read(path: str, mtime_ns: int, size: int) -> Bindings:
    del mtime_ns, size  # the cache key: a rewritten file is read again
    try:
        tree = ast.parse(Path(path).read_bytes(), filename=path)
    except (OSError, SyntaxError, ValueError):
        return Bindings(frozenset(), dynamic=True)
    names: set[str] = set()
    dynamic = False
    for node in _in_module_scope(tree):
        names.update(_names_bound(node))
        dynamic |= _serves_names(node)
    if path.endswith("__init__.py"):
        names.add("__path__")  # the import system sets it on a package
    return Bindings(frozenset(names), dynamic)


def _names_bound(node: ast.AST) -> list[str]:
    """The module-level names one node binds."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name, *_declared_global(node)]
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Store) else []
    if isinstance(node, ast.Import):
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [a.asname or a.name for a in node.names]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _serves_names(node: ast.AST) -> bool:
    """Whether one node lets the module answer for names it does not bind."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name == "__getattr__"
    if isinstance(node, ast.ImportFrom):
        return any(a.name == "*" for a in node.names)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "globals"
    return False


def _in_module_scope(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node that runs in the module's own namespace: nested scopes are yielded, not entered."""
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _declared_global(function: ast.AST) -> Iterator[str]:
    """Names a function declares `global`: it binds them in the module when it runs."""
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return
    for node in ast.walk(function):
        if isinstance(node, ast.Global):
            yield from node.names
