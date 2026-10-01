#!/usr/bin/env bash
# -*- shell-script -*-
# The one way to get a development worktree: creates it, then bootstraps it —
# its own real .venv, the locked install, `npm ci` for ui/web, and the shared
# git-hook / editable-venv guards.
#
#   bash scripts/setup-worktree.sh <task> [--branch NAME] [--base REF]
#       Create <main clone>/.worktrees/<task> on branch ava-<task> (or <task>
#       itself when it already starts with ava-) off a freshly fetched
#       origin/main, then bootstrap it. Run it from the main clone or from any
#       worktree; the new one always lands under the main clone's .worktrees/.
#   bash scripts/setup-worktree.sh
#       Bootstrap the worktree this script lives in (also how to complete a
#       worktree made by another tool, e.g. Claude Code's own under
#       .claude/worktrees/). Refused in the main clone.
#
# Idempotent: an existing worktree is only re-bootstrapped. On success the last
# stdout line is `worktree ready: <absolute path> (branch <branch>)`; a script
# cannot change its caller's directory, so `cd` there afterwards.
set -euo pipefail

die() { echo "✖ $*" >&2; exit 1; }

TASK="" BRANCH="" BASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --branch) [ $# -ge 2 ] || die "--branch needs a value"; BRANCH="$2"; shift 2 ;;
    --base) [ $# -ge 2 ] || die "--base needs a value"; BASE="$2"; shift 2 ;;
    -h|--help) sed -n '3,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (usage: setup-worktree.sh [<task> [--branch NAME] [--base REF]])" ;;
    *) [ -z "$TASK" ] || die "unexpected argument: $1 (one <task> only)"; TASK="$1"; shift ;;
  esac
done
if [ -z "$TASK" ] && { [ -n "$BRANCH" ] || [ -n "$BASE" ]; }; then
  die "--branch and --base only apply when creating: give a <task>"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir)"
MAIN_ROOT="$(cd -P "$(dirname "$COMMON_DIR")" && pwd -P)"  # physical, as `git worktree list` prints it

# A shell that leaked another checkout's venv must never steer uv or the guard.
unset VIRTUAL_ENV

for tool in git uv npm python3; do
  command -v "$tool" >/dev/null || die "$tool is not on PATH — install it (or use a login shell) before creating a worktree"
done

# Is $1 a registered worktree of this repository? (awk reads everything: no SIGPIPE under pipefail.)
is_worktree() {
  git -C "$MAIN_ROOT" worktree list --porcelain \
    | awk -v want="worktree $1" '$0 == want { found = 1 } END { exit !found }'
}

# The worktree that has branch $1 checked out, if any.
checked_out_at() {
  git -C "$MAIN_ROOT" worktree list --porcelain \
    | awk -v want="branch refs/heads/$1" '/^worktree /{ path = substr($0, 10) } $0 == want && !seen { print path; seen = 1 }'
}

create_worktree() {
  [[ $TASK =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || die "task '$TASK' must be a plain directory name (letters, digits, '.', '_', '-'; no slashes)"
  local wanted_branch="$BRANCH" holder current
  if [ -z "$BRANCH" ]; then
    case "$TASK" in ava-*) BRANCH="$TASK" ;; *) BRANCH="ava-$TASK" ;; esac
  fi
  WORKTREE="$MAIN_ROOT/.worktrees/$TASK"

  if is_worktree "$WORKTREE"; then
    [ -d "$WORKTREE" ] || die "$WORKTREE is registered as a worktree but its directory is gone — run: git worktree prune"
    current="$(git -C "$WORKTREE" branch --show-current)"
    if [ -n "$wanted_branch" ] && [ "$current" != "$wanted_branch" ]; then
      die "$WORKTREE already exists on branch '${current:-detached HEAD}', not '$wanted_branch' — pick another <task>, or drop --branch"
    fi
    echo "→ worktree $WORKTREE already exists; bootstrapping it"
    return
  fi
  if [ -e "$WORKTREE" ] || [ -L "$WORKTREE" ]; then
    die "$WORKTREE exists but is not a worktree of this repository — move or delete it, or pick another <task>"
  fi
  if git -C "$MAIN_ROOT" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    holder="$(checked_out_at "$BRANCH")"
    [ -z "$holder" ] || die "branch '$BRANCH' is already checked out at $holder — use that worktree, or pick another <task> / --branch"
    die "branch '$BRANCH' already exists (checked out nowhere) — pick another <task> / --branch, or delete the stale branch first: git branch -d $BRANCH"
  fi

  BASE="${BASE:-origin/main}"
  case "$BASE" in
    origin/*)
      echo "→ fetching $BASE …"
      git -C "$MAIN_ROOT" fetch --quiet origin "+refs/heads/${BASE#origin/}:refs/remotes/$BASE" \
        || die "cannot fetch $BASE — check the network, or pass --base with a ref you already have"
      ;;
  esac
  git -C "$MAIN_ROOT" rev-parse --verify --quiet "$BASE^{commit}" >/dev/null \
    || die "base '$BASE' is not a commit in this repository"

  echo "→ creating worktree $WORKTREE (branch $BRANCH from $BASE) …"
  git -C "$MAIN_ROOT" worktree add --no-track -b "$BRANCH" "$WORKTREE" "$BASE"
}

bootstrap() {
  cd "$REPO_ROOT"
  trap 'rc=$?; [ "$rc" -eq 0 ] || echo "✖ bootstrap of $REPO_ROOT stopped (exit $rc): fix the error above, then run \`bash scripts/setup-worktree.sh\` inside it — it resumes where it stopped" >&2' EXIT
  local before branch
  before="$(git status --porcelain)"

  echo "→ shared git hook installation check …"
  # Installation belongs to the main clone's stable venv: all worktrees share
  # hooks, and pre-commit stores the installing interpreter in INSTALL_PYTHON.
  python3 "$SCRIPT_DIR/provision/check_git_hooks.py" --strict

  echo "→ editable venv guard …"
  python3 "$SCRIPT_DIR/host_ops/guard_editable_venv.py" "$REPO_ROOT"

  if [ ! -x .venv/bin/python ]; then
    echo "→ creating this worktree's own .venv …"
    uv sync --frozen
  fi

  echo "→ locked Python install …"
  .venv/bin/python "$REPO_ROOT/cli/python_install.py" --locked --inexact

  # `npm ci`, as CI does: it installs exactly what package-lock.json pins and never
  # rewrites it (`npm install` strips optional-dependency entries on a newer npm).
  echo "→ npm ci (frontend) …"
  (cd "$REPO_ROOT/ui/web" && npm ci --no-audit --no-fund)

  echo "→ verifying …"
  { [ -d .venv ] && [ ! -L .venv ]; } || die ".venv must be a real directory inside $REPO_ROOT, never a symlink"
  [ -x .venv/bin/python ] || die ".venv/bin/python is missing"
  python3 "$SCRIPT_DIR/host_ops/guard_editable_venv.py" "$REPO_ROOT"
  if [ "$(git status --porcelain)" != "$before" ]; then
    git status --short >&2
    die "bootstrap changed tracked files (a rewritten uv.lock or package-lock.json?) — inspect the diff above and report it"
  fi

  branch="$(git branch --show-current)"
  echo "worktree ready: $REPO_ROOT (branch ${branch:-detached HEAD})"
}

if [ -n "$TASK" ]; then
  create_worktree
  # Bootstrap with the new worktree's own copy: this one may be a stale checkout's.
  [ -f "$WORKTREE/scripts/setup-worktree.sh" ] \
    || die "$WORKTREE has no scripts/setup-worktree.sh to bootstrap it with — rebase it onto a newer base"
  exec bash "$WORKTREE/scripts/setup-worktree.sh"
fi
if [ "$REPO_ROOT" = "$MAIN_ROOT" ]; then
  die "$REPO_ROOT is the main clone — pass a <task> to create a worktree (bash scripts/setup-worktree.sh <task>), or run this inside an existing worktree"
fi
bootstrap
