#!/usr/bin/env bash
# Run a command at push only if the branch (merge-base origin/main..HEAD) added, changed,
# deleted or renamed a path that matches an extended regular expression.
#
# A whole-repository check whose verdict can only move when certain files move (a path that
# is deleted counts, which pre-commit's own file selection never passes to a hook) does not
# need to run on a push that touches none of them. An unknown range fails with
# the shared base error;
# not knowing is not a reason to claim an empty contribution.
#
#   prepush-if-changed.sh '\.py$' -- .venv/bin/python scripts/lint/patch_targets.py
set -euo pipefail

pattern="${1:?usage: prepush-if-changed.sh ERE -- command...}"
shift
[[ "${1:-}" == -- && $# -ge 2 ]] || { echo "Expected -- command..." >&2; exit 2; }
shift

base_sha="$(bash "$(dirname "$0")/prepush-base.sh")"
paths="$(git diff --name-only --no-renames "$base_sha" HEAD)"
status=0
grep -Eq -- "$pattern" <<< "$paths" || status=$?
[[ "$status" -le 1 ]] || exit "$status"
if [[ "$status" == 1 ]]; then
    echo "pre-push: no path on this branch matches '$pattern'; skipping"
    exit 0
fi
exec "$@"
