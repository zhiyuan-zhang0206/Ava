#!/usr/bin/env bash
# =============================================================================
# Sweeper standalone mechanical-scan runner for the Ava repo — cron-friendly,
# zero-agent.
#
# Add to crontab to periodically scan for tech debt. This script runs only the
# 5 greppable / tool-checkable debt classes (deps, fail-fast, inline-marker,
# dead-code, docstring-budget detection) of the 8 in SKILL.md and writes a
# report to stdout. The other 3
# reasoned classes (docs-aging, boundary, skill-desc) need agent judgment and
# are intentionally omitted here. (import-lint graduated to a blocking pre-commit
# hook, `lint-imports`, so it is no longer a sweeper class.) It does NOT open a
# PR — that requires the AI agent (see SKILL.md + ava.skills.sweeper).
#
# Usage:
#   bash .agents/skills/ava-sweeper/run.sh [--repo <path>]
#
# Cron example (weekly, Sunday 03:00):
#   0 3 * * 0 bash /path/to/Ava/.agents/skills/ava-sweeper/run.sh >> ~/logs/sweep.log 2>&1
# =============================================================================

set -euo pipefail

if [[ $# -eq 0 ]]; then
    REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
elif [[ "$1" == "--repo" && $# -eq 2 && -n "$2" ]]; then
    REPO="$2"
else
    echo "usage: run.sh [--repo <path>]" >&2
    exit 2
fi
cd "$REPO"

echo "=== Sweeper mechanical scan $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "repo: $REPO"
echo "HEAD: $(git rev-parse --short HEAD)"
echo ""

SCAN_DIRS="ava/ plugins/ agent/ gateway/ cli/ services/ shared/"

# ------------------------------------------------------------------
# Class 1: outdated deps
# ------------------------------------------------------------------
echo "--- [1/5] deps: uv pip list --outdated ---"
uv pip list --outdated 2>&1 || echo "(uv pip list failed)"
echo ""

if [ -d frontend/node_modules ]; then
    echo "--- [1/5] deps: npm outdated (frontend) ---"
    (cd frontend && npm outdated --json 2>&1) || echo "(npm outdated failed)"
    echo ""
else
    echo "--- [1/5] deps: npm outdated SKIPPED (no node_modules) ---"
    echo ""
fi

# ------------------------------------------------------------------
# Class 3: fail-fast anti-patterns
# ------------------------------------------------------------------
echo "--- [2/5] fail-fast: .get(k) or {} ---"
rg -n '\.get\([^)]*\)\s+or\s+\{' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

echo "--- [2/5] fail-fast: case _: defaults ---"
rg -n 'case\s+_\s*:' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

echo "--- [2/5] fail-fast: rare / shouldn't happen / almost never comments ---"
rg -n -i "(rare|shouldn't happen|almost never)" $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

echo "--- [2/5] fail-fast: except Exception: pass ---"
rg -n 'except\s+Exception\s*:\s*pass' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

# ------------------------------------------------------------------
# Class 4: inline markers (TODO/FIXME/XXX/HACK)
# ------------------------------------------------------------------
echo "--- [3/5] inline-marker: TODO|FIXME|XXX|HACK ---"
rg -n 'TODO|FIXME|XXX|HACK' $SCAN_DIRS 2>&1 || echo "(none found)"
echo ""

# ------------------------------------------------------------------
# Class 5: dead code (vulture)
# ------------------------------------------------------------------
echo "--- [4/5] dead-code: vulture ---"
uvx vulture $SCAN_DIRS --min-confidence 80 2>&1 || echo "(vulture failed or not installed)"
echo ""

# ------------------------------------------------------------------
# Class 8: docstring-budget (detection half — judgement happens in the sweep)
# ------------------------------------------------------------------
echo "--- [5/5] docstring-budget: Raises sections + soft-zone lengths ---"
.venv/bin/python - <<'PY' 2>&1 || echo "(docstring-budget scan failed)"
import ast
from pathlib import Path
from scripts.lint_agent_docstrings import (
    _discover_plugin_namespace_modules, _is_in_scope,
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
hits = 0
for f in sorted(root.rglob("*.py")):
    if ".venv" in f.parts or not _is_in_scope(f.relative_to(root), ns_files):
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

echo "=== Scan complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
