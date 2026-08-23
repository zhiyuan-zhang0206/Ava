#!/usr/bin/env bash
#
# deploy/lgtm/stop.sh — stop the native LGTM jobs only.
# For a deliberate stop on the designated host, use
# `ava lgtm off`: it removes the marker FIRST so the watchdog cannot resurrect
# the backends while this script is taking them down. The container rollback
# path is manual: restore the prior compose + backend configs from git, then
# run `docker compose up -d`. Retained volumes remain rollback assets.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

for name in loki prometheus; do
    launchctl bootout "gui/$(id -u)/com.ava.$name" || true
done
echo "native LGTM jobs stopped (retained compose data volumes remain rollback assets)"
