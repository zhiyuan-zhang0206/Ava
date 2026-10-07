#!/bin/bash
# Check that base/events/registry.md is in sync with the event contract
# registry (base/events/contract.py EVENTS + live_events SSE roles).
# Writes to temp + diffs — never mutates the actual file, so pre-commit's
# stash/restore never sees a spurious mtime change.
#
# On drift, exit 1 + tell the user to regenerate.

set -e

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

cd "$(dirname "$0")/../.."  # repo root

.venv/bin/python scripts/codegen/gen_event_registry.py "$TMPDIR/registry.md" >/dev/null

if ! diff -q base/events/registry.md "$TMPDIR/registry.md" >/dev/null; then
    echo "ERROR: base/events/registry.md is out of sync with the event contract registry"
    echo "   run .venv/bin/python scripts/codegen/gen_event_registry.py to regenerate"
    diff base/events/registry.md "$TMPDIR/registry.md" | head -30
    exit 1
fi

echo "base/events/registry.md is in sync"
