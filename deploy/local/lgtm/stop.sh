#!/usr/bin/env bash
#
# deploy/local/lgtm/stop.sh — stop the AVA local trace viewer.
# Data volumes persist; start.sh brings it back with all history intact.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

docker compose down
echo "viewer stopped (volumes kept). wipe history with: docker compose down -v"
