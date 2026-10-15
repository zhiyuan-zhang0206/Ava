#!/bin/bash
# Check that types-generated.ts + openapi.json are in sync with current
# Pydantic schemas — does not mutate actual files (writes to temp + diff),
# compatible with the pre-commit hook's stash/restore (mutating actual
# files, even with identical content, changes mtime and the hook still
# flags them as "modified").
#
# On drift, exit 1 + tell the user to run `./scripts/codegen/codegen-types.sh` to regenerate.

set -e

types_scratch=$(mktemp -d)
trap "rm -rf $types_scratch" EXIT

cd "$(dirname "$0")/../.."  # repo root

.venv/bin/python scripts/codegen/dump_openapi.py "$types_scratch/openapi.json" >/dev/null
node scripts/codegen/typescript-types.mjs "$types_scratch/openapi.json" "$types_scratch/types-generated.ts"

if ! diff -q ui/web/openapi.json "$types_scratch/openapi.json" >/dev/null; then
    echo "ERROR: ui/web/openapi.json is out of sync with Pydantic schema"
    echo "   run ./scripts/codegen/codegen-types.sh to regenerate"
    diff ui/web/openapi.json "$types_scratch/openapi.json" | head -30
    exit 1
fi

if ! diff -q ui/web/src/lib/contracts/types-generated.ts "$types_scratch/types-generated.ts" >/dev/null; then
    echo "ERROR: ui/web/src/lib/contracts/types-generated.ts is out of sync with OpenAPI spec"
    echo "   run ./scripts/codegen/codegen-types.sh to regenerate"
    diff ui/web/src/lib/contracts/types-generated.ts "$types_scratch/types-generated.ts" | head -30
    exit 1
fi
