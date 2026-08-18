#!/usr/bin/env bash
# -*- shell-script -*-
# Worktree management for ~/Ava repo.
# Usage:
#   scripts/worktree.sh create <task-name>   # create branch + worktree from main
#   scripts/worktree.sh clean  <task-name>   # remove worktree + delete branch
#   scripts/worktree.sh list                 # list all worktrees
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WORKTREE_ROOT="$REPO_ROOT/.worktrees"

# ── helpers ──────────────────────────────────────────────────────────────────

die() { echo "✖ $*" >&2; exit 1; }
ok()  { echo "✓ $*"; }

ensure_main() {
    # Fetch and make sure main exists
    git -C "$REPO_ROOT" fetch origin main 2>/dev/null || true
    if ! git -C "$REPO_ROOT" rev-parse --verify origin/main >/dev/null 2>&1; then
        die "remote branch 'origin/main' not found"
    fi
    # Update local main from origin/main without switching branches
    git -C "$REPO_ROOT" branch -f main origin/main 2>/dev/null ||
        git -C "$REPO_ROOT" branch main origin/main 2>/dev/null || true
}

# ── commands ─────────────────────────────────────────────────────────────────

cmd_create() {
    local task="$1"
    local branch="ava/${task}"
    local wt_path="$WORKTREE_ROOT/$task"

    # Validate task name early
    if [[ "$task" =~ [[:space:]/] ]]; then
        die "task name must not contain whitespace or slashes"
    fi

    ensure_main

    if [[ -d "$wt_path" ]]; then
        die "worktree already exists: $wt_path"
    fi

    if git -C "$REPO_ROOT" rev-parse --verify "$branch" >/dev/null 2>&1; then
        die "branch already exists: $branch"
    fi

    echo "→ creating branch $branch from main …"
    git -C "$REPO_ROOT" branch "$branch" main

    echo "→ adding worktree at $wt_path …"
    git -C "$REPO_ROOT" worktree add "$wt_path" "$branch"

    ok "worktree created: $wt_path  (branch: $branch)"

    echo "→ running setup-worktree.sh …"
    bash "$wt_path/scripts/setup-worktree.sh"
    ok "setup complete"
}

cmd_clean() {
    local task="$1"
    local branch="ava/${task}"
    local wt_path="$WORKTREE_ROOT/$task"

    if [[ ! -d "$wt_path" ]]; then
        die "worktree not found: $wt_path"
    fi

    echo "→ removing worktree $wt_path …"
    git -C "$REPO_ROOT" worktree remove "$wt_path" || {
        echo "→ force-removing worktree …"
        git -C "$REPO_ROOT" worktree remove --force "$wt_path"
    }
    ok "worktree removed"

    if git -C "$REPO_ROOT" rev-parse --verify "$branch" >/dev/null 2>&1; then
        echo "→ deleting branch $branch …"
        git -C "$REPO_ROOT" branch -D "$branch"
        ok "branch deleted: $branch"
    else
        echo "→ branch $branch not found (already deleted?)"
    fi
}

cmd_list() {
    echo "Worktrees under $WORKTREE_ROOT:"
    echo ""

    if [[ ! -d "$WORKTREE_ROOT" ]] || [[ -z "$(ls -A "$WORKTREE_ROOT" 2>/dev/null)" ]]; then
        echo "  (none)"
        return
    fi

    for d in "$WORKTREE_ROOT"/*/; do
        local name="$(basename "$d")"
        local branch=""
        branch=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
        local dirty=""
        if ! git -C "$d" diff-index --quiet HEAD -- 2>/dev/null; then
            dirty=" [dirty]"
        fi
        printf "  %-30s  branch: %s%s\n" "$name" "$branch" "$dirty"
    done

    echo ""
    echo "Git worktree list:"
    git -C "$REPO_ROOT" worktree list
}

# ── main ─────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Usage: worktree.sh <command> [args]

Commands:
  create <task-name>   Create branch ava/<task> + worktree from main, then run setup
  clean  <task-name>   Remove worktree and delete branch ava/<task>
  list                 List all worktrees under .worktrees/
EOF
    exit 1
}

case "${1:-}" in
    create) shift; cmd_create "${1:?usage: worktree.sh create <task-name>}" ;;
    clean)  shift; cmd_clean  "${1:?usage: worktree.sh clean <task-name>}" ;;
    list)   cmd_list ;;
    *)      usage ;;
esac
