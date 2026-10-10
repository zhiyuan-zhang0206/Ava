"""Only the CLI database composition owner declares exempt admission."""

from __future__ import annotations

import ast
from pathlib import Path


def _declares_exempt_gate(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    if name != "ProcessDbGate":
        return False
    return any(
        arg.arg == "exempt" and isinstance(arg.value, ast.Constant) and arg.value.value is True
        for arg in node.keywords
    )


def test_only_the_cli_database_owner_declares_exempt_admission() -> None:
    """A service that declared it would be the writer the gate exists to stop,
    running unchecked. Services start with `python -m <module>`, never through here."""
    root = Path(__file__).resolve().parents[3]
    skipped = {"tests", "ui", "docs", "node_modules", "assets", "demos"}
    callers: set[str] = set()
    for top in sorted(root.iterdir()):
        if top.name.startswith(".") or top.name in skipped or not top.is_dir():
            continue
        for path in sorted(top.rglob("*.py")):
            if "tests" in path.relative_to(root).parts:
                continue  # a package's own `tests/` is not production code
            text = path.read_text()
            if "ProcessDbGate" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if _declares_exempt_gate(node):
                    callers.add(path.relative_to(root).as_posix())
    assert callers == {"cli/database.py"}
