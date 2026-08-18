#!/usr/bin/env bash
# -*- shell-script -*-
# Pre-push check — run local tests before allowing push to remote.
# Install: ln -s ../../scripts/pre-push-check.sh .git/hooks/pre-push
# Or run manually: bash scripts/pre-push-check.sh
#
# Detects changed files and runs the appropriate test suite:
# - Python changes  → .venv/bin/pytest (touched files)
# - Frontend changes → vitest + eslint + tsc
# - Mixed           → both
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# If pre-push hook: stdin has "<local ref> <local sha1> <remote ref> <remote sha1>"
# We read the range of commits being pushed
HAS_PYTHON=0
HAS_FRONTEND=0
EXIT_CODE=0

if [ $# -ge 2 ]; then
    # Running as git hook — check commits being pushed
    LOCAL_SHA="$2"
    REMOTE_SHA="$4"

    if [ "$REMOTE_SHA" = "0000000000000000000000000000000000000000" ]; then
        # Branch being deleted, nothing to check
        exit 0
    fi

    CHANGED=$(git diff --name-only "$REMOTE_SHA" "$LOCAL_SHA" 2>/dev/null) || CHANGED=""
else
    # Running manually — check staged + unstaged changes
    CHANGED=$(git diff --name-only HEAD 2>/dev/null) || CHANGED=""
fi

if echo "$CHANGED" | grep -q '\.py$'; then
    HAS_PYTHON=1
fi
if echo "$CHANGED" | grep -q '^ui/web/'; then
    HAS_FRONTEND=1
fi

# --- Python checks ---
if [ "$HAS_PYTHON" -eq 1 ]; then
    echo "→ Python changes detected — running pytest …"
    # Run tests in the touched areas (pass specific test files for speed)
    PYTEST_FILES=$(echo "$CHANGED" | grep '\.py$' | sed 's|/.*||' | sort -u | while read -r dir; do
        # Map source dirs to test dirs
        case "$dir" in
            agent)     echo "tests/agent/" ;;
            gateway)   echo "tests/gateway/" ;;
            shared)    echo "tests/shared/" ;;
            ava)       echo "tests/ava/" ;;
            services)  echo "tests/services/" ;;
            cli)       echo "tests/cli/" ;;
            plugins)   echo "tests/plugins/" ;;
            *)         ;;
        esac
    done | sort -u)

    if [ -n "$PYTEST_FILES" ]; then
        # shellcheck disable=SC2086
        .venv/bin/pytest $PYTEST_FILES -q || EXIT_CODE=1
    else
        echo "  (no matching test dirs, skipping pytest)"
    fi
fi

# --- Frontend checks ---
if [ "$HAS_FRONTEND" -eq 1 ]; then
    echo "→ Frontend changes detected — running vitest + eslint + tsc …"

    cd ui/web

    echo "  vitest …"
    npx --no-install vitest run || EXIT_CODE=1

    echo "  eslint …"
    npx --no-install eslint . --max-warnings 0 || EXIT_CODE=1

    echo "  tsc …"
    npx --no-install next typegen && npx --no-install tsc --noEmit || EXIT_CODE=1

    cd "$REPO_ROOT"
fi

if [ "$HAS_PYTHON" -eq 0 ] && [ "$HAS_FRONTEND" -eq 0 ]; then
    echo "→ No Python or frontend changes detected — skipping tests."
fi

if [ "$EXIT_CODE" -ne 0 ]; then
    echo ""
    echo "✗ Local tests failed. Fix failures before pushing."
    echo "  To bypass (not recommended): git push --no-verify"
    exit 1
fi

echo "✓ Local tests passed."
