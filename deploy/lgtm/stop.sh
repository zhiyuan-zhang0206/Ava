#!/usr/bin/env bash
#
# deploy/lgtm/stop.sh — stop the hybrid observability backend.
# Data volumes persist. For a deliberate stop on the designated host, use
# `ava lgtm off`: it removes the marker FIRST so the watchdog cannot resurrect
# the backends while this script is taking them down. To roll back to the
# container backends, stop here, restore the prior compose + backend configs
# from git, then run `docker compose up -d`; the retained volumes are the
# rollback assets.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

for name in loki prometheus promtail; do
    launchctl bootout "gui/$(id -u)/com.ava.$name" || true
done
docker compose down
echo "stack stopped (volumes kept). wipe history with: docker compose down -v"
