#!/usr/bin/env bash
# -*- shell-script -*-
# Worktree management for ~/Ava repo.
# Usage:
#   scripts/worktree.sh create <task-name>           # create branch + worktree from main
#   scripts/worktree.sh clean  <task-name> [--force] # remove worktree + delete branch
#   scripts/worktree.sh list                         # list all worktrees
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
    local force="${2:-}"

    if [[ "$task" == -* ]]; then
        die "missing task name (usage: worktree.sh clean <task-name> [--force])"
    fi
    if [[ -n "$force" && "$force" != "--force" ]]; then
        die "unknown option: $force (usage: worktree.sh clean <task-name> [--force])"
    fi

    local branch="ava/${task}"
    local wt_path="$WORKTREE_ROOT/$task"
    local guard_script="$REPO_ROOT/scripts/check_worktree_remove.py"

    if [[ ! -d "$wt_path" ]]; then
        die "worktree not found: $wt_path"
    fi

    # Two branch conventions clean must cover (task #3710): worktree.sh's own
    # (ava/<task>) and agent-created worktrees, which name branch == dir
    # (ava-<id>-<slug>). Prefer the tool's name; fall back to the agent one so
    # neither convention leaves a branch behind.
    if [[ "$task" == ava-* ]] \
        && ! git -C "$REPO_ROOT" rev-parse --verify "$branch" >/dev/null 2>&1 \
        && git -C "$REPO_ROOT" rev-parse --verify "refs/heads/$task" >/dev/null 2>&1; then
        branch="$task"
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
        echo "→ no branch found for $task (already deleted?)"
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
  create <task-name>            Create branch ava/<task> + worktree from main, then run setup
  clean  <task-name> [--force]  Remove worktree and delete its branch (ava/<task> or ava-<id>-<slug>)
  list                          List all worktrees under .worktrees/

clean is anchor-guarded: it refuses when live sessions or processes are still
anchored under the worktree (scripts/check_worktree_remove.py), when that
check cannot run (no python with psutil, or the checker missing), or when git
cannot remove the tree. --force overrides explicitly: the anchor check becomes
a warning (skipped entirely when it cannot run) and a failed removal is retried
with git worktree remove --force.
EOF
    exit 1
}

case "${1:-}" in
    create) shift; cmd_create "${1:?usage: worktree.sh create <task-name>}" ;;
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
