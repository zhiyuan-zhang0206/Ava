#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
git ls-files -z | node scripts/content_lint/code_comments/typescript.mjs > "$scratch/typescript.json"
if [[ "${1:-}" == changed && $# == 1 && -n "${PRE_COMMIT_FROM_REF:-}${PRE_COMMIT_TO_REF:-}" ]]; then
  set -- changed --from-ref "${PRE_COMMIT_FROM_REF:?missing from-ref}" --to-ref "${PRE_COMMIT_TO_REF:?missing to-ref}"
fi
.venv/bin/python -m scripts.content_lint.code_comments --typescript-index "$scratch/typescript.json" "$@"
