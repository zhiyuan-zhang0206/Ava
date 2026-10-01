#!/usr/bin/env bash
# -*- shell-script -*-
# Idempotent worktree bootstrap. Run once after `git worktree add`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "→ shared git hook installation check …"
cd "$REPO_ROOT"
# Installation belongs to the main clone's stable venv: all worktrees share
# hooks, and pre-commit stores the installing interpreter in INSTALL_PYTHON.
python3 "$SCRIPT_DIR/provision/check_git_hooks.py"

echo "→ editable venv guard …"
python3 "$SCRIPT_DIR/host_ops/guard_editable_venv.py" "$REPO_ROOT"

if [ ! -x .venv/bin/python ]; then
  echo "→ creating this worktree's own .venv …"
  env -u VIRTUAL_ENV uv sync --frozen
fi

echo "→ locked Python install …"
env -u VIRTUAL_ENV .venv/bin/python "$REPO_ROOT/cli/python_install.py" --locked --inexact

# `npm ci`, as CI does: it installs exactly what package-lock.json pins and never
# rewrites it (`npm install` strips optional-dependency entries on a newer npm).
echo "→ npm ci (frontend) …"
cd "$REPO_ROOT/ui/web"
npm ci --no-audit --no-fund

cd "$REPO_ROOT"
echo "✓ worktree ready"
