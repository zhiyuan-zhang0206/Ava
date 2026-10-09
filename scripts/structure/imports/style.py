"""Qualified import style facts, without an active repository gate.

The repository's top-level Python package is the style boundary. Normalization
still supplies the actual dependency target; style never grants layer or private
access. Direct-script loaders and bare sibling bindings need their invocation
contracts migrated before a scanner can enforce this rule across the tree.
"""

from __future__ import annotations

import ast

from . import Clause, normalize, package_of


def _internal_absolute_targets(
    node: ast.Import | ast.ImportFrom, clause: Clause, top: str
) -> list[str]:
    if isinstance(node, ast.ImportFrom) and node.level:
        return []
    targets = [clause.base] if clause.base else [b.target for b in clause.bindings]
    return sorted({target for target in targets if target.split(".")[0] == top})


def errors(tree: ast.AST, rel_path: str) -> list[tuple[int, str]]:
    """Style errors of actual import statements in a package-relative source path.

    Nested sibling packages share their top-level boundary. Standalone modules
    have no package boundary. Imports inside sample strings are not statements;
    runtime import strings and bare names are not guessed into package targets.
    An empty result does not establish those loaders' compliance.
    Invalid relative imports propagate the shared file-and-line diagnostic.
    """
    package = package_of(rel_path)
    top = package[0] if package and package[0].isidentifier() else None
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        clause = normalize(node, rel_path)
        internal = _internal_absolute_targets(node, clause, top) if top is not None else []
        if internal:
            targets = ", ".join(f"`{target}`" for target in internal)
            found.append(
                (
                    clause.line,
                    f"absolute import within top-level package `{top}`: {targets}; "
                    "use an explicit relative import and preserve its bound names",
                )
            )
    return sorted(found)
