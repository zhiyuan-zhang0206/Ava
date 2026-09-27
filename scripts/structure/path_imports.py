"""Path imports: code under ava_builtins/ reaches other code through packages, never a file path.

A skill or plugin script that edits `sys.path`, calls `site.addsitedir`, or loads
a module from a file (`importlib.util.spec_from_file_location`,
`importlib.machinery.SourceFileLoader`, `runpy.run_path`) turns its directory into
an unreviewed code package that sidesteps the package doors, budgets and locality
rules. Shared code belongs in a governed package the script imports normally, and
the script stays a thin entry point. Rule 6 in scripts/lint_code_structure.py; the
frozen sites live in the `path_imports` section of scripts/structure/baseline.json
as `path::target -> site count`, matched exactly like the locality sections.
"""

from __future__ import annotations

import ast

SECTION = "path_imports"
_SCOPE = ("ava_builtins/",)
FIX = (
    "move the shared code into a governed package (ava/, shared/, or the plugin's own "
    "package) and import it normally; keep the script a thin entry point"
)
_PATH_MUTATORS = frozenset({"insert", "append", "extend", "remove", "pop", "clear"})
# Loader callable -> the site key it is frozen under.
_LOADERS = {
    "spec_from_file_location": "importlib.util.spec_from_file_location",
    "SourceFileLoader": "importlib.machinery.SourceFileLoader",
    "run_path": "runpy.run_path",
    "addsitedir": "site.addsitedir",
}
Sites = dict[str, list[int]]


class _Bindings:
    """Local names bound to the sys module, to `sys.path` itself, and to a path loader."""

    def __init__(self, tree: ast.Module) -> None:
        self.sys_names: set[str] = set()
        self.path_names: set[str] = set()
        self.loaders: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.sys_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "sys"
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module == "sys" and alias.name == "path":
                        self.path_names.add(local)
                    elif alias.name in _LOADERS:
                        self.loaders[local] = _LOADERS[alias.name]

    def is_sys_path(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Subscript):
            node = node.value
        if isinstance(node, ast.Name):
            return node.id in self.path_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys_names
        )

    def call_target(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.loaders.get(func.id)
        if not isinstance(func, ast.Attribute):
            return None
        if func.attr in _PATH_MUTATORS and self.is_sys_path(func.value):
            return "sys.path"
        return _LOADERS.get(func.attr)


def _assigned(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign | ast.Delete):
        return list(node.targets)
    if isinstance(node, ast.AugAssign | ast.AnnAssign):
        return [node.target]
    return []


def measure(tree: ast.Module, rel_path: str) -> Sites:
    """Path-import sites in one module under `ava_builtins/`, keyed `path::target`."""
    if not rel_path.startswith(_SCOPE):
        return {}
    bindings = _Bindings(tree)
    sites: Sites = {}
    for node in ast.walk(tree):
        targets: list[tuple[int, str]] = []
        if isinstance(node, ast.Call) and (target := bindings.call_target(node)) is not None:
            targets.append((node.lineno, target))
        targets.extend(
            (node.lineno, "sys.path") for expr in _assigned(node) if bindings.is_sys_path(expr)
        )
        for lineno, target in targets:
            sites.setdefault(f"{rel_path}::{target}", []).append(lineno)
    return sites
