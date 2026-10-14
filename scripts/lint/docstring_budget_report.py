"""Debt-candidate report, docstring-budget class: Raises sections and soft-zone lengths.

`python -m scripts.lint.docstring_budget_report` from the repository root, as the docstring-budget
section of `tech_debt_candidates.sh`. Detection only; judgement happens in the sweep. Prints one line
per finding, or `(none found)`.
"""

import ast
import sys
from pathlib import Path

from scripts.lint.agent_docstrings import (
    discover_agent_surface_modules,
    discover_plugin_namespace_modules,
    is_agent_visible,
    is_in_scope,
    plugin_wrap_targets,
    static_agent_visible_names,
)


def doc_of(
    node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[str | None, int | None]:
    """The raw docstring and the line of its expression, or (None, None)."""
    doc = ast.get_docstring(node, clean=False)
    return (doc, node.body[0].lineno) if doc is not None else (None, None)


def file_findings(path: Path) -> list[str]:
    """The findings of one in-scope file."""
    tree = ast.parse(path.read_text())
    rel = str(path)
    is_plugin = rel.endswith("/plugin.py")
    all_names = static_agent_visible_names(tree)
    wraps: set[str] = plugin_wrap_targets(tree) if is_plugin else set()
    found: list[str] = []
    if not is_plugin:
        doc, line = doc_of(tree)
        if doc and len(doc.splitlines()) > 2:
            found.append(
                f"{rel}:{line}: module docstring {len(doc.splitlines())} lines (soft cap 2)"
            )
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        visible = (node.name in wraps) if is_plugin else is_agent_visible(node.name, all_names)
        doc, line = doc_of(node) if visible else (None, None)
        if not doc:
            continue
        if "Raises:" in doc:
            found.append(f"{rel}:{line}: {node.name} has a Raises: section")
        if len(doc.splitlines()) > 12:
            found.append(
                f"{rel}:{line}: {node.name} docstring {len(doc.splitlines())} lines (soft cap 12)"
            )
    return found


def findings(root: Path) -> list[str]:
    ns_files = discover_plugin_namespace_modules(root)
    surface_files = discover_agent_surface_modules(root)
    found: list[str] = []
    for f in sorted(root.rglob("*.py")):
        if ".venv" in f.parts or not is_in_scope(f.relative_to(root), ns_files, surface_files):
            continue
        found.extend(file_findings(f))
    return found


def main() -> None:
    lines = findings(Path())
    sys.stdout.write("".join(f"{line}\n" for line in lines) or "(none found)\n")


if __name__ == "__main__":
    main()
