#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
git ls-files -z | node scripts/content_lint/code_comments/typescript.mjs > "$scratch/typescript.json"
if [[ "${1:-}" == changed ]]; then
  shift
  bash scripts/content_lint/code_comments/changed.sh "$@" > "$scratch/changed"
  .venv/bin/python -m scripts.content_lint.code_comments --typescript-index "$scratch/typescript.json" --changed-paths "$scratch/changed" check
else
  .venv/bin/python -m scripts.content_lint.code_comments --typescript-index "$scratch/typescript.json" "$@"
fi
