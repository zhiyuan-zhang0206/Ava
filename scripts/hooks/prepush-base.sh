#!/usr/bin/env bash
# One contribution range for every push selector, independent of old remote tips.
set -euo pipefail
if ! git rev-parse --verify -q origin/main >/dev/null; then
    echo "ERROR: pre-push requires local origin/main; fetch origin main before pushing" >&2
    exit 1
fi
if ! base=$(git merge-base origin/main HEAD) || [[ -z "$base" ]]; then
    echo "ERROR: pre-push cannot establish a merge-base with origin/main" >&2
    exit 1
fi
printf '%s\n' "$base"
