#!/usr/bin/env bash
set -euo pipefail
if [[ $# -gt 1 ]]; then
  echo "Usage: changed.sh [base-ref]" >&2
  exit 2
fi
if [[ $# == 1 ]]; then
  base="$(git rev-parse --verify --end-of-options "$1^{commit}")"
  git diff --name-only -z --diff-filter=ACMRT "$base" HEAD --
elif [[ -n "${PRE_COMMIT_FROM_REF:-}${PRE_COMMIT_TO_REF:-}" ]]; then
  base="$(git rev-parse --verify --end-of-options "${PRE_COMMIT_FROM_REF:?missing from-ref}^{commit}")"
  target="$(git rev-parse --verify --end-of-options "${PRE_COMMIT_TO_REF:?missing to-ref}^{commit}")"
  if [[ "$target" != "$(git rev-parse HEAD)" ]]; then
    echo "Code comment range must end at the checked-out HEAD" >&2
    exit 2
  fi
  git diff --name-only -z --diff-filter=ACMRT "$base" "$target" --
else
  git diff --cached --name-only -z --diff-filter=ACMRT --
fi
