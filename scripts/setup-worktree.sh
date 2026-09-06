#!/usr/bin/env bash
# -*- shell-script -*-
# Idempotent worktree bootstrap. Run once after `git worktree add`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "→ pre-commit hooks …"
cd "$REPO_ROOT"
# pre-commit refuses to install if core.hooksPath is configured at all,
# even when it points to the default .git/hooks. Unset it so the install
# can proceed — the default path is correct.
git config --local --unset core.hookspath 2>/dev/null || true
.venv/bin/pre-commit install --install-hooks

echo "→ editable venv guard …"
python3 "$SCRIPT_DIR/guard_editable_venv.py" "$REPO_ROOT"

echo "→ locked Python install …"
env -u VIRTUAL_ENV .venv/bin/python "$REPO_ROOT/cli/python_install.py" --locked --inexact

echo "→ npm install (frontend) …"
cd "$REPO_ROOT/ui/web"
npm install

cd "$REPO_ROOT"
echo "✓ worktree ready"
