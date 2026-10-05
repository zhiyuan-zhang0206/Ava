#!/usr/bin/env bash
# Re-run every pre-commit-stage hook over the WHOLE branch diff
# (merge-base origin/main..HEAD -> HEAD), not just whatever two endpoints
# pre-commit's own push-time selection would pick. `git rebase` / `cherry-pick`
# / `merge` never invoke the pre-commit hook for the commits they create
# (confirmed empirically against this host's git — not a config gap), so a
# conflict-resolution commit, or a cherry-picked commit, can otherwise reach
# `git push` having never been checked by a single pre-commit-stage hook.
#
# This closes that gap by nesting a nested `pre-commit run` invocation scoped
# to the real branch diff. It does NOT close the separate delete-only gap
# (a files:-filtered hook never sees a purely deleted path, on any range) —
# see scripts/provision/prepush_freshness.py for that companion fix.
#
# The commit-stage hooks judge only the files they are handed, so this run costs about
# what one commit over the same files costs. That is why it takes no load threshold and no
# lock (unlike the heavy tools behind scripts/prepush-guard.sh): it is light enough to run
# every time, and a load-dependent skip would leave rebased commits unchecked at random.
#
# Missing range fails explicitly; a missing pre-commit executable still reports
# an unverified local run. A local skip is never evidence the check ran; CI
# (backend-structure / merged-tree-structure) independently re-verifies the pushed and
# merged tree.
set -euo pipefail

skip() {
    echo "WARNING: PRE-PUSH SKIPPED [branch-lint]: $*. CI must pass before merge." >&2
    exit 0
}

base_sha="$(bash "$(dirname "$0")/prepush-base.sh")"

[[ -x .venv/bin/pre-commit ]] || skip "missing .venv/bin/pre-commit; run env -u VIRTUAL_ENV uv sync"

exec .venv/bin/pre-commit run --hook-stage pre-commit --from-ref "$base_sha" --to-ref HEAD
