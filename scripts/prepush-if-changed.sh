#!/usr/bin/env bash
# Run a command at push only if the branch (merge-base origin/main..HEAD) added, changed,
# deleted or renamed a path that matches an extended regular expression.
#
# A whole-repository check whose verdict can only move when certain files move (a path that
# is deleted counts, which pre-commit's own file selection never passes to a hook) does not
# need to run on a push that touches none of them. When the range cannot be known (no
# origin/main), the command runs: not knowing is not a reason to skip.
#
#   prepush-if-changed.sh '\.py$' -- .venv/bin/python scripts/lint/patch_targets.py
set -euo pipefail

pattern="${1:?usage: prepush-if-changed.sh ERE -- command...}"
shift
[[ "${1:-}" == -- && $# -ge 2 ]] || { echo "Expected -- command..." >&2; exit 2; }
shift

if git rev-parse --verify -q origin/main >/dev/null 2>&1 \
    && base_sha="$(git merge-base origin/main HEAD 2>/dev/null)" && [[ -n "$base_sha" ]]; then
    # --no-renames: a rename is a deletion of the old path plus an addition of the new one.
    if ! git diff --name-only --no-renames "$base_sha" HEAD | grep -Eq -- "$pattern"; then
        echo "pre-push: no path on this branch matches '$pattern'; skipping"
        exit 0
    fi
fi
exec "$@"
