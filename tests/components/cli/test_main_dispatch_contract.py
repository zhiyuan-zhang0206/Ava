"""Contract: scans the repository root for entry points that declare the database gate exemption."""

from __future__ import annotations

import ast
from pathlib import Path


def test_only_the_cli_entry_point_declares_the_database_gate_exemption() -> None:
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
            if "exempt_from_db_gate" not in text:
                continue
            for node in ast.walk(ast.parse(text)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "exempt_from_db_gate"
                ):
                    callers.add(path.relative_to(root).as_posix())
    assert callers == {"cli/main.py"}
