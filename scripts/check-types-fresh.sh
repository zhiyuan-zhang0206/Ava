#!/bin/bash
# Check that types-generated.ts + openapi.json are in sync with current
# Pydantic schemas — does not mutate actual files (writes to temp + diff),
# compatible with the pre-commit hook's stash/restore (mutating actual
# files, even with identical content, changes mtime and the hook still
# flags them as "modified").
#
# On drift, exit 1 + tell the user to run `./scripts/codegen-types.sh` to regenerate.

set -e

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

cd "$(dirname "$0")/.."  # repo root

.venv/bin/python scripts/dump_openapi.py "$TMPDIR/openapi.json" >/dev/null
(cd frontend && npx --no-install openapi-typescript "$TMPDIR/openapi.json" -o "$TMPDIR/types-generated.ts" >/dev/null)

if ! diff -q frontend/openapi.json "$TMPDIR/openapi.json" >/dev/null; then
    echo "ERROR: frontend/openapi.json is out of sync with Pydantic schema"
    echo "   run ./scripts/codegen-types.sh to regenerate"
    diff frontend/openapi.json "$TMPDIR/openapi.json" | head -30
    exit 1
fi

if ! diff -q frontend/src/lib/types-generated.ts "$TMPDIR/types-generated.ts" >/dev/null; then
    echo "ERROR: frontend/src/lib/types-generated.ts is out of sync with OpenAPI spec"
    echo "   run ./scripts/codegen-types.sh to regenerate"
    diff frontend/src/lib/types-generated.ts "$TMPDIR/types-generated.ts" | head -30
    exit 1
fi
