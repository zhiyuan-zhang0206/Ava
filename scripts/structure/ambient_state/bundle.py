"""The bundle-leak rule: a `@root_bundle` class never leaves the module that defines it.

A composition root may gather what it wires into one frozen dataclass marked
`base.config.wiring.root_bundle` and unpack it locally. Annotating that class anywhere else, as a
parameter, a return value or an attribute, hands library code the whole bundle, so it can reach
any member: a service locator under another name. In every module but the defining one, such an
annotation (resolved through the module's `from ... import`) is a site, frozen like the other
ambient-state sites as `path::bundle-leak:<Class>`; the files that define bundles are the only
ones that may name them. A re-export through a second module is not followed (a known gap).
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

from scripts.structure.ambient_state.scan import Hit

BUNDLE_LEAK = "bundle-leak"
FIX = (
    "a `@root_bundle` class stays in its composition root: unpack it there and pass the members "
    "each component needs (handles, slices) as arguments"
)
_DECORATOR = "root_bundle"


def _is_bundle(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(d, ast.Name) and d.id == _DECORATOR)
        or (isinstance(d, ast.Attribute) and d.attr == _DECORATOR)
        for d in node.decorator_list
    )


@cache
def _bundles_defined_in(path: Path) -> frozenset[str]:
    """The `@root_bundle` class names a source file defines."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return frozenset()
    return frozenset(
        n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and _is_bundle(n)
    )


def _module_file(repo_root: Path, module: str) -> Path | None:
    base = repo_root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_bundles(tree: ast.Module, repo_root: Path) -> set[str]:
    """Local names the module imports that are bundle classes."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module):
            continue
        source = _module_file(repo_root, node.module)
        if source is None:
            continue
        defined = _bundles_defined_in(source)
        found.update(a.asname or a.name for a in node.names if a.name in defined)
    return found


def _annotations(tree: ast.Module) -> list[ast.expr]:
    out: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            out += [a.annotation for a in every if a is not None and a.annotation is not None]
            if node.returns is not None:
                out.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            out.append(node.annotation)
    return out


def hits(tree: ast.Module, repo_root: Path) -> list[Hit]:
    """Every annotation of a bundle class imported from another module."""
    bundles = _imported_bundles(tree, repo_root)
    if not bundles:
        return []
    return [
        Hit(BUNDLE_LEAK, node.id, node.lineno)
        for annotation in _annotations(tree)
        for node in ast.walk(annotation)
        if isinstance(node, ast.Name) and node.id in bundles
    ]
