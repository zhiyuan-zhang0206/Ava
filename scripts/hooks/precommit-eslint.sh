#!/usr/bin/env bash
# ESLint over the frontend files a commit changed, through the same warning gate as
# `npm run lint` (errors and unbaselined warnings fail).
#
# The lint setup itself -- the config, the local rules, the warning baseline, tsconfig and the
# dependencies -- can change the verdict of files that did not change, so an edit to any of
# them lints the whole project, as does `--all-files`. A type-aware rule can also react to a
# type that changed in ANOTHER file, which this per-file run does not see: the pre-push
# hook checks branch paths; CI's frontend job lints the whole project.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [[ ! -x ui/web/node_modules/.bin/eslint ]]; then
    echo "WARNING: ESLINT SKIPPED: missing ui/web/node_modules; run (cd ui/web && npm ci). CI must pass before merge." >&2
    exit 0
fi

full=0
files=()
for path in "$@"; do
    case "$path" in
        ui/web/eslint.config.mjs | ui/web/eslint-rules/* | ui/web/scripts/check-eslint-warnings.mjs \
            | ui/web/scripts/eslint-warning-baseline.json | ui/web/package.json \
            | ui/web/package-lock.json | ui/web/tsconfig.json)
            full=1
            ;;
        *) files+=("${path#ui/web/}") ;;
    esac
done

cd ui/web
if [[ "$full" == 1 ]]; then
    exec npm run lint
fi
[[ ${#files[@]} -gt 0 ]] || exit 0
# --no-warn-ignored: a file the config ignores is a clean pass here, not a null-rule warning.
npx --no-install eslint --no-warn-ignored --format json "${files[@]}" | node scripts/check-eslint-warnings.mjs
