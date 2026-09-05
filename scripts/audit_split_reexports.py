"""Audit module-split re-exports across the whole repo (task #2498 W-series).

Run: `.venv/bin/python scripts/audit_split_reexports.py <module> [<module> ...]`
from the repo root, e.g. the W3 split surface:

    .venv/bin/python scripts/audit_split_reexports.py shared.events.contract shared.migrations

## Why

Splitting an oversized module moves definitions into shard modules and keeps
the origin module as a re-export facade. The AST byte-identity check that
proves moved definitions unchanged covers def/class bodies only — module-level
constants (and other names) are not in that diff. A missed re-export is an
ImportError that only surfaces at runtime, in a burst (PR #1729 QA BLOCK:
`UNANCHORED_DB_SENTINEL` missed by `shared.db`'s re-export list failed 11
shards). This script is the split's reference-completeness gate.

## What it checks

For every origin module, collect every name the repo references in three
forms — `from <module> import X`, `<module>.X` attribute access, and
monkeypatch string targets `"<module>.X"` / `'<module>.X'` — then verify each
name exists as an attribute of the imported module. Any missing name is a
missed re-export and exits non-zero.

The origin module itself and `.venv`/`node_modules` trees are excluded.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _referenced_names(module: str) -> set[str]:
    """Every name the repo references on `module`, in all three forms."""
    refs: set[str] = set()
    attr_re = re.compile(rf"(?:from\s+)?{re.escape(module)}\.(\w+)")
    string_re = re.compile(rf"[\"']{re.escape(module)}\.(\w+)[\"']")
    origin = (_REPO / f"{module.replace('.', '/')}.py").resolve()
    for py in _REPO.rglob("*.py"):
        if ".venv" in py.parts or "node_modules" in py.parts:
            continue
        if py.resolve() == origin:
            continue
        try:
            text = py.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module:
                refs.update(a.name for a in node.names)
        for line in text.splitlines():
            refs.update(m.group(1) for m in attr_re.finditer(line))
            refs.update(m.group(1) for m in string_re.finditer(line))
    return refs


def _missing(module: str, refs: set[str]) -> list[str]:
    loaded = importlib.import_module(module)
    return sorted(name for name in refs if not hasattr(loaded, name))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("## What it checks")[0])
    parser.add_argument(
        "modules",
        nargs="+",
        metavar="MODULE",
        help="origin (facade) module to audit, e.g. shared.migrations",
    )
    args = parser.parse_args(argv)
    failed = False
    for module in args.modules:
        refs = _referenced_names(module)
        missing = _missing(module, refs)
        print(f"{module}: {len(refs)} referenced names, missing={missing}")
        failed = failed or bool(missing)
    print("AUDIT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
