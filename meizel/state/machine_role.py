"""Rule: the retired ambient machine-role reader.

Machine capabilities belong to an explicit MachineIdentity supplied by the
composition root. The retired `machine_role()` call is rejected in every governed
production module; there is no per-file allowance. Capability guards read the
owner's `role()` and retain the single gateway routing contract (issue #216).
"""

from __future__ import annotations

import ast

__all__ = ["violations"]


def _calls(tree: ast.AST) -> list[int]:
    """Line numbers of `machine_role(...)` call sites in a parsed module."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "machine_role"
        ):
            hits.append(node.lineno)
    return hits


def violations(tree: ast.AST) -> list[tuple[int, str]]:
    """`(line, message)` for every `machine_role()` call of a governed module."""
    return [
        (
            lineno,
            "machine_role() is retired; pass an explicit MachineIdentity owner "
            "and read its role(). The gateway remains the single routing point "
            "(issue #216).",
        )
        for lineno in _calls(tree)
    ]
