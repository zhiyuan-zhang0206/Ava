#!/usr/bin/env bash
# pyright at push time only type-checks the branch's own changed .py files,
# never the whole repository. A full-repo pyright is CI-only (user ruling
# 2026-09-22: local runs check only the files touched; full-repo/full-suite
# runs are reserved for CI). Scope matches scripts/prepush-branch-lint.sh's
# range: merge-base(origin/main, HEAD)..HEAD.
set -euo pipefail

skip() {
    echo "WARNING: PRE-PUSH SKIPPED [pyright]: $*. CI must pass before merge." >&2
    exit 0
}

command -v git >/dev/null || skip "git is not installed"
git rev-parse --verify -q origin/main >/dev/null 2>&1 \
    || skip "origin/main is not resolvable locally; fetch first for full local coverage (CI still checks the pushed tree)"

base_sha="$(git merge-base origin/main HEAD 2>/dev/null)" \
    || skip "could not compute 'git merge-base origin/main HEAD'"
[[ -n "$base_sha" ]] || skip "empty merge-base with origin/main"

# --diff-filter=ACMR (Added/Copied/Modified/Renamed) matches pre-commit's own
# selection; a deleted .py file cannot be handed to pyright, and a renamed
# path's OLD name would already be excluded by the filter. -z / NUL-delimited
# for exact paths (spaces, etc.).
files=()
while IFS= read -r -d '' file; do
    [[ "$file" == *.py ]] || continue
    [[ -f "$file" ]] || continue
    files+=("$file")
done < <(git diff --name-only --diff-filter=ACMR -z "$base_sha" HEAD)

if [[ ${#files[@]} -eq 0 ]]; then
    echo "pre-push: no changed .py files on this branch (merge-base origin/main..HEAD); skipping pyright"
    exit 0
fi

exec bash scripts/prepush-guard.sh pyright -- .venv/bin/pyright "${files[@]}"
