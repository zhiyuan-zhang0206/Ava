#!/bin/bash
# Check that frontend/src/lib/constants-generated.ts is in sync with the
# backend constant source of truth (shared/live_events.py EVENT_COALESCE_MS).
# Does not mutate actual files (writes to temp + diff), compatible with the
# pre-commit hook's stash/restore (mutating actual files, even with identical
# content, changes mtime and the hook still flags them as "modified").
#
# On drift, exit 1 + tell the user to run ./scripts/dump_frontend_constants.py.

set -e

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

cd "$(dirname "$0")/.."  # repo root

.venv/bin/python scripts/dump_frontend_constants.py "$TMPDIR" >/dev/null

if ! diff -q frontend/src/lib/constants-generated.ts "$TMPDIR/constants-generated.ts" >/dev/null; then
    echo "ERROR: frontend/src/lib/constants-generated.ts is out of sync with shared/live_events.py"
    echo "   run ./scripts/dump_frontend_constants.py to regenerate"
    diff frontend/src/lib/constants-generated.ts "$TMPDIR/constants-generated.ts" | head -30
    exit 1
fi
