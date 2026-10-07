#!/usr/bin/env bash
# -*- shell-script -*-
# Worktree teardown and listing for the ~/Ava repo. Creating a worktree is
# scripts/setup-worktree.sh <task>: the one entry point that also bootstraps it.
# Usage:
#   scripts/worktree.sh clean  <task-name> [--force] # remove worktree + delete branch
#   scripts/worktree.sh list                         # list all worktrees
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WORKTREE_ROOT="$REPO_ROOT/.worktrees"

# ── helpers ──────────────────────────────────────────────────────────────────

die() { echo "✖ $*" >&2; exit 1; }
ok()  { echo "✓ $*"; }

# ── commands ─────────────────────────────────────────────────────────────────

cmd_clean() {
    local task="$1"
    local force="${2:-}"

    if [[ "$task" == -* ]]; then
        die "missing task name (usage: worktree.sh clean <task-name> [--force])"
    fi
    if [[ -n "$force" && "$force" != "--force" ]]; then
        die "unknown option: $force (usage: worktree.sh clean <task-name> [--force])"
    fi

    # The branch setup-worktree.sh names after the task: ava-<task>, or <task>
    # itself when it already starts with ava-. A custom --branch is left alone.
    local branch="ava-${task}"
    if [[ "$task" == ava-* ]]; then
        branch="$task"
    fi
    local wt_path="$WORKTREE_ROOT/$task"
    local guard_script="$REPO_ROOT/scripts/host_ops/check_worktree_remove.py"

    if [[ ! -d "$wt_path" ]]; then
        die "worktree not found: $wt_path"
    fi

    # Live-anchor guard (issue #194): a removal kills every session/process
    # whose cwd lies under the worktree, so run the checker shipped with THIS
    # checkout (the tool version decides) and refuse unless it comes back
    # clean. The checker needs a python that can import psutil — the
    # worktree's own venv first, then the repo venv. When neither is usable
    # (or the checker is missing), fail safe: refuse unless --force.
    local guard_python=""
    local cand
    for cand in "$wt_path/.venv/bin/python" "$REPO_ROOT/.venv/bin/python"; do
        if [[ -x "$cand" ]] && "$cand" -c 'import psutil' >/dev/null 2>&1; then
            guard_python="$cand"
            break
        fi
    done

    local guard_problem=""
    if [[ ! -f "$guard_script" ]]; then
        guard_problem="live-anchor checker missing: $guard_script"
    elif [[ -z "$guard_python" ]]; then
        guard_problem="no python with psutil to run the live-anchor checker (tried $wt_path/.venv and $REPO_ROOT/.venv)"
    fi

    if [[ -n "$guard_problem" ]]; then
        if [[ "$force" == "--force" ]]; then
            echo "⚠ $guard_problem — live-anchor check SKIPPED (--force)" >&2
        else
            die "$guard_problem — refusing removal (re-run with --force to override)"
        fi
    else
        local guard_rc=0
        local guard_out=""
        guard_out="$("$guard_python" "$guard_script" "$wt_path" 2>&1)" || guard_rc=$?
        if [[ $guard_rc -ne 0 ]]; then
            if [[ "$force" == "--force" ]]; then
                echo "⚠ live-anchor check did not pass (rc=$guard_rc) — removing anyway (--force):" >&2
                echo "$guard_out" >&2
            elif [[ "$guard_out" == REFUSE* ]]; then
                echo "$guard_out" >&2
                die "removal refused: live anchor(s) under $wt_path (re-run with --force to override)"
            else
                echo "$guard_out" >&2
                die "live-anchor check failed — removal refused (re-run with --force to override)"
            fi
        else
            ok "live-anchor check passed"
        fi
    fi

    # Never force-discards on its own: a failed removal (dirty tree, lock)
    # leaves everything in place unless --force asks for the destructive retry.
    echo "→ removing worktree $wt_path …"
    if ! git -C "$REPO_ROOT" worktree remove "$wt_path"; then
        if [[ "$force" == "--force" ]]; then
            echo "→ force-removing worktree (--force) …"
            git -C "$REPO_ROOT" worktree remove --force "$wt_path"
        else
            die "removal failed (dirty worktree?) — re-run with --force to discard local changes"
        fi
    fi
    ok "worktree removed"

    if git -C "$REPO_ROOT" rev-parse --verify "$branch" >/dev/null 2>&1; then
        echo "→ deleting branch $branch …"
        git -C "$REPO_ROOT" branch -D "$branch"
        ok "branch deleted: $branch"
    else
        echo "→ no branch $branch to delete (already deleted, or a custom --branch left in place)"
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
  clean  <task-name> [--force]  Remove worktree and delete its branch (ava-<task>, or <task> when it starts with ava-)
  list                          List all worktrees under .worktrees/

To create a worktree, run: bash scripts/setup-worktree.sh <task-name>

clean is anchor-guarded: it refuses when live sessions or processes are still
anchored under the worktree (scripts/host_ops/check_worktree_remove.py), when that
check cannot run (no python with psutil, or the checker missing), or when git
cannot remove the tree. --force overrides explicitly: the anchor check becomes
a warning (skipped entirely when it cannot run) and a failed removal is retried
with git worktree remove --force.
EOF
    exit 1
}

case "${1:-}" in
    clean)
        shift
        if [[ $# -gt 2 ]]; then
            die "too many arguments (usage: worktree.sh clean <task-name> [--force])"
        fi
        cmd_clean "${1:?usage: worktree.sh clean <task-name> [--force]}" "${2:-}"
        ;;
    list)   cmd_list ;;
    *)      usage ;;
esac
