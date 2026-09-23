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
python3 "$SCRIPT_DIR/guard_editable_venv.py" "$REPO_ROOT"

echo "→ locked Python install …"
env -u VIRTUAL_ENV .venv/bin/python "$REPO_ROOT/cli/python_install.py" --locked --inexact

echo "→ npm install (frontend) …"
cd "$REPO_ROOT/ui/web"
npm install

cd "$REPO_ROOT"
echo "✓ worktree ready"
