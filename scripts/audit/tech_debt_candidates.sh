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

# Tool errors make the report incomplete; findings are successful observations.
SCAN_ERRORS=0
SCAN_TMP=$(mktemp -d)
trap 'rm -rf "$SCAN_TMP"' EXIT

report_scan() {
    local mode="$1" code=0 outcome
    shift
    "$@" >"$SCAN_TMP/stdout" 2>"$SCAN_TMP/stderr" || code=$?
    cat "$SCAN_TMP/stdout"
    cat "$SCAN_TMP/stderr"
    case "$mode:$code" in
        rg:0|vulture:3) outcome=findings ;;
        rg:1|vulture:0) outcome=empty ;;
        text:0)
            if [[ ! -s "$SCAN_TMP/stdout" ]] || [[ $(cat "$SCAN_TMP/stdout") == "(none found)" ]]; then
                outcome=empty
            else
                outcome=findings
            fi ;;
        uv:0|npm:0|npm:1|cochange:0)
            outcome=$(.venv/bin/python - "$mode" "$SCAN_TMP/stdout" <<'JSON'
import json
import sys
from pathlib import Path

mode, source = sys.argv[1:]
try:
    value = json.loads(Path(source).read_text())
    if mode == "uv":
        if not isinstance(value, list) or any(
            not isinstance(row, dict) or not all(isinstance(row.get(key), str)
            for key in ("name", "version", "latest_version")) for row in value
        ):
            raise ValueError("expected an outdated-package list")
        findings = bool(value)
    elif mode == "npm":
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("expected an outdated-package object, not an error")
        for rows in value.values():
            for row in rows if isinstance(rows, list) else [rows]:
                if not isinstance(row, dict) or not all(
                    isinstance(row.get(key), str) for key in ("wanted", "latest")
                ):
                    raise ValueError("invalid outdated-package record")
        findings = bool(value)
    else:
        if not isinstance(value, dict) or not isinstance(value["strong_pairs"], list):
            raise ValueError("expected locality report with strong_pairs")
        count = value["fix_wide_count"]
        if type(count) is not int or count < 0:
            raise ValueError("invalid locality fix_wide_count")
        findings = bool(count or value["strong_pairs"])
    print("findings" if findings else "empty")
except (ValueError, KeyError, TypeError, OSError) as exc:
    print(f"report parse error: {exc}", file=sys.stderr)
    sys.exit(1)
JSON
            ) || outcome=error
            if [[ "$mode" == npm && "$code:$outcome" != "0:empty" && "$code:$outcome" != "1:findings" ]]; then
                echo "npm exit code and report disagree: $code / $outcome"
                outcome=error
            fi ;;
        *) outcome=error ;;
    esac
    echo "[result: $outcome; exit: $code]"
    if [[ "$outcome" == error ]]; then SCAN_ERRORS=$((SCAN_ERRORS + 1)); fi
}

npm_report() { (cd ui/web && npm outdated --json); }

SCAN_DIRS="ava/ ava_builtins/ agent/ gateway/ cli/ ops/ schedules/ services/ base/"

# ------------------------------------------------------------------
# Class 1: outdated deps
# ------------------------------------------------------------------
echo "--- [1/6] deps: uv pip list --outdated ---"
report_scan uv uv pip list --outdated --format json
echo ""

if [ -d ui/web/node_modules ]; then
    echo "--- [1/6] deps: npm outdated (frontend) ---"
    report_scan npm npm_report
    echo ""
else
    echo "--- [1/6] deps: npm outdated SKIPPED (no node_modules) ---"
    echo "[result: skipped; reason: no node_modules]"
    echo ""
fi

# ------------------------------------------------------------------
# Class 3: fail-fast anti-patterns
# ------------------------------------------------------------------
echo "--- [2/6] fail-fast: .get(k) or {} ---"
report_scan rg rg -n '\.get\([^)]*\)\s+or\s+\{' $SCAN_DIRS
echo ""

echo "--- [2/6] fail-fast: case _: defaults ---"
report_scan rg rg -n 'case\s+_\s*:' $SCAN_DIRS
echo ""

echo "--- [2/6] fail-fast: rare / shouldn't happen / almost never comments ---"
report_scan rg rg -n -i "(rare|shouldn't happen|almost never)" $SCAN_DIRS
echo ""

# ------------------------------------------------------------------
# Class 4: inline markers (TODO/FIXME/XXX/HACK)
# ------------------------------------------------------------------
echo "--- [3/6] inline-marker: TODO|FIXME|XXX|HACK ---"
report_scan rg rg -n 'TODO|FIXME|XXX|HACK' $SCAN_DIRS
echo ""

# ------------------------------------------------------------------
# Class 5: dead code (vulture)
# ------------------------------------------------------------------
echo "--- [4/6] dead-code: vulture ---"
report_scan vulture uvx vulture $SCAN_DIRS --min-confidence 80
echo ""

# ------------------------------------------------------------------
# Class 8: docstring-budget (detection half — judgement happens in the sweep)
# ------------------------------------------------------------------
echo "--- [5/6] docstring-budget: Raises sections + soft-zone lengths ---"
report_scan text .venv/bin/python - <<'PY'
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
report_scan cochange .venv/bin/python scripts/structure/cochange.py --json
echo ""

if [[ "$SCAN_ERRORS" -gt 0 ]]; then
    echo "=== Scan incomplete: $SCAN_ERRORS section error(s) $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    exit 1
fi
echo "=== Scan complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
