from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from base.events.contract import EVENTS

_REPO = Path(__file__).resolve().parents[3]
_CALLEES = {"emit", "emit_prepared", "prepare_event"}
_CATEGORIES = {"audit", "telemetry", "log"}


def _production_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [
        _REPO / name
        for name in tracked
        if "/tests/" not in name and not name.startswith(("tests/", "scripts/", "meizel/"))
    ]


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def literal_emit_sites(source: str) -> list[tuple[int, str, str]]:
    sites: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if callee not in _CALLEES or len(node.args) < 2:
            continue
        category, name = _literal(node.args[0]), _literal(node.args[1])
        if category in _CATEGORIES and name is not None:
            sites.append((node.lineno, category, name))
    return sites


def test_the_scan_recognises_the_call_shapes_in_use() -> None:
    source = (
        "from base import telemetry\n"
        "from base.telemetry import emit\n"
        "telemetry.emit('telemetry', 'a')\n"
        "emit('audit', 'b', attributes={})\n"
        "telemetry.emit(\n    'telemetry',\n    'c',\n    attributes={},\n)\n"
        "telemetry.emit('telemetry', name)\n"
        "other.emit('not-a-category', 'd')\n"
    )
    assert literal_emit_sites(source) == [
        (3, "telemetry", "a"),
        (4, "audit", "b"),
        (5, "telemetry", "c"),
    ]


def test_every_literal_emit_site_names_a_declared_event_in_a_declared_category() -> None:
    undeclared: list[str] = []
    mismatched: list[str] = []
    scanned = 0
    for path in _production_files():
        for line, category, name in literal_emit_sites(path.read_text(encoding="utf-8")):
            scanned += 1
            where = f"{path.relative_to(_REPO)}:{line}: {category}/{name}"
            spec = EVENTS.get(name)
            if spec is None:
                undeclared.append(where)
            elif category != spec.category and category not in spec.extra_categories:
                mismatched.append(f"{where} (declared {spec.category})")
    assert scanned > 30, "the scan found almost no call sites: it is looking in the wrong place"
    assert undeclared == [], (
        "emit() raises for these event names: declare them in base/events/declarations"
    )
    assert mismatched == [], "emit() raises for these categories: they contradict the declaration"
