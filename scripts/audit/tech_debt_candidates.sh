#!/usr/bin/env bash
# Optional Ava debt-candidate report. Rules and evidence requirements live in
# docs/conventions/tech-debt.md. This is not a lint or contribution prerequisite.
# Usage: bash scripts/audit/tech_debt_candidates.sh [--repo PATH]
# The existing daily-debt schedule consumes this report; no runtime is changed here.

set -euo pipefail

if [[ $# -eq 0 ]]; then
    REPO="$(cd "$(dirname "$0")/../.." && pwd)"
elif [[ "$1" == "--repo" && $# -eq 2 && -n "$2" ]]; then
    REPO="$2"
else
    echo "usage: tech_debt_candidates.sh [--repo <path>]" >&2
    exit 2
fi
cd "$REPO"

echo "=== Sweeper mechanical scan $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "repo: $REPO"
echo "HEAD: $(git rev-parse --short HEAD)"
echo ""

SCAN_DIRS="ava/ ava_builtins/ agent/ gateway/ cli/ ops/ schedules/ services/ base/"

# ------------------------------------------------------------------
# Class 1: outdated deps
# ------------------------------------------------------------------
echo "--- [1/6] deps: uv pip list --outdated ---"
uv pip list --outdated 2>&1 || echo "(uv pip list failed)"
echo ""

if [ -d ui/web/node_modules ]; then
    echo "--- [1/6] deps: npm outdated (frontend) ---"
    (cd ui/web && npm outdated --json 2>&1) || echo "(npm outdated failed)"
    echo ""
else
    echo "--- [1/6] deps: npm outdated SKIPPED (no node_modules) ---"
    echo ""
fi

# ------------------------------------------------------------------
# Class 3: fail-fast anti-patterns
# ------------------------------------------------------------------
echo "--- [2/6] fail-fast: .get(k) or {} ---"
rg -n '\.get\([^)]*\)\s+or\s+\{' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

echo "--- [2/6] fail-fast: case _: defaults ---"
rg -n 'case\s+_\s*:' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

echo "--- [2/6] fail-fast: rare / shouldn't happen / almost never comments ---"
rg -n -i "(rare|shouldn't happen|almost never)" $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

# ------------------------------------------------------------------
# Class 4: inline markers (TODO/FIXME/XXX/HACK)
# ------------------------------------------------------------------
echo "--- [3/6] inline-marker: TODO|FIXME|XXX|HACK ---"
rg -n 'TODO|FIXME|XXX|HACK' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

# ------------------------------------------------------------------
# Class 5: dead code (vulture)
# ------------------------------------------------------------------
echo "--- [4/6] dead-code: vulture ---"
uvx vulture $SCAN_DIRS --min-confidence 80 2>&1 || echo "(vulture failed or not installed)"
echo ""

# ------------------------------------------------------------------
# Class 8: docstring-budget (detection half — judgement happens in the sweep)
# ------------------------------------------------------------------
echo "--- [5/6] docstring-budget: Raises sections + soft-zone lengths ---"
.venv/bin/python - <<'PY' 2>&1 || echo "(docstring-budget scan failed)"
import ast
from pathlib import Path
from scripts.lint.agent_docstrings import (
    _discover_agent_surface_modules, _discover_plugin_namespace_modules, _is_in_scope,
    _agent_visible_names, _wrap_targets, _is_visible,
)

def doc_of(node):
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        return node.body[0].value.value, node.body[0].lineno
    return None, None

root = Path(".")
ns_files = _discover_plugin_namespace_modules(root)
surface_files = _discover_agent_surface_modules(root)
hits = 0
for f in sorted(root.rglob("*.py")):
    if ".venv" in f.parts or not _is_in_scope(f.relative_to(root), ns_files, surface_files):
        continue
    tree = ast.parse(f.read_text())
    rel = str(f)
    is_plugin = rel.endswith("/plugin.py")
    allnames = _agent_visible_names(tree)
    wraps = _wrap_targets(tree) if is_plugin else set()
    if not is_plugin:
        d, ln = doc_of(tree)
        if d and len(d.splitlines()) > 2:
            print(f"{rel}:{ln}: module docstring {len(d.splitlines())} lines (soft cap 2)")
            hits += 1
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visible = (node.name in wraps) if is_plugin else _is_visible(node.name, allnames)
        if not visible:
            continue
        d, ln = doc_of(node)
        if not d:
            continue
        if "Raises:" in d:
            print(f"{rel}:{ln}: {node.name} has a Raises: section")
            hits += 1
        if len(d.splitlines()) > 12:
            print(f"{rel}:{ln}: {node.name} docstring {len(d.splitlines())} lines (soft cap 12)")
            hits += 1
if not hits:
    print("(none found)")
PY
echo ""

# ------------------------------------------------------------------
# Class 11: locality (whole-repo) — per-commit spread + cross-package
# co-change index, defaults (90-day window on main, min-support 8,
# min-confidence 0.6). Detection only; findings + ledger entries need
# judgment (docs/conventions/tech-debt.md).
# ------------------------------------------------------------------
echo "--- [6/6] locality: cochange.py (spread + co-change index) ---"
.venv/bin/python scripts/structure/cochange.py 2>&1 || echo "(cochange scan failed)"
echo ""

echo "=== Scan complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
