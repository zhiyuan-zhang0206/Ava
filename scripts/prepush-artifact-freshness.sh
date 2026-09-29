#!/usr/bin/env bash
# Always-run duplicate of the generated-artifact / snapshot freshness family,
# ignoring every files: filter.
#
# pre-commit's own diff selection passes a files:-filtered hook only
# Added/Copied/Modified/Renamed paths -- never a Deleted one (confirmed
# empirically: `git diff --diff-filter=ACMR` is what pre-commit actually
# selects on, and a delete-only diff produces an empty selection regardless
# of the compared range). So a change that only deletes the last file
# referencing a public symbol, event, or config field can leave the
# corresponding generated snapshot stale with no pre-commit-stage hook ever
# firing -- on any range, including the branch-diff rerun in
# scripts/prepush-branch-lint.sh. That is exactly how PR #3658 reached CI
# with a stale base/agents/api.txt after a rebase deleted the module it
# described.
#
# This hook re-checks the whole repository at every push instead, by
# re-invoking each underlying hook's own --all-files behavior. The underlying
# hooks keep their normal filtered, pre-commit-stage-only behavior (see
# .pre-commit-config.yaml) so `git commit` stays fast; this list is the
# unconditional pre-push-only duplicate.
set -euo pipefail

# Keep in sync with lint-prepush-artifact-freshness's rationale comment in
# .pre-commit-config.yaml; tests/ci/test_prepush_hooks.py checks the pairing.
hooks=(
    lint-contract-snapshots
    types-codegen-fresh
    constants-codegen-fresh
    events-registry-fresh
    config-lite-table-fresh
    lint-ava-okf
    check-doc-references
)

status=0
for hook in "${hooks[@]}"; do
    # types-codegen-fresh shells out to `npx --no-install openapi-typescript`
    # (scripts/check-types-fresh.sh), which needs ui/web/node_modules -- the
    # same frontend-tooling dependency frontend-tsc/eslint/vitest already
    # skip on via scripts/prepush-guard.sh. Everything else here is pure
    # Python and needs nothing beyond the venv this pre-commit run is already
    # executing under, so only this one sub-check gets the same treatment.
    if [[ "$hook" == types-codegen-fresh && ! -d ui/web/node_modules ]]; then
        echo "WARNING: PRE-PUSH SKIPPED [$hook]: missing ui/web/node_modules; run (cd ui/web && npm ci). CI must pass before merge." >&2
        continue
    fi
    .venv/bin/pre-commit run --hook-stage pre-commit --all-files "$hook" || status=1
done
exit "$status"
