#!/usr/bin/env bash
#
# deploy/lgtm/stop.sh — stop the observability stack.
# Data volumes persist; start.sh brings it back with all history intact.
#
# On the designated LGTM host ($AVA_HOME/lgtm-host marker) the gateway
# watchdog re-runs start.sh within ~a minute of the probes failing — for a
# deliberate stop there use `ava lgtm off`, which removes the marker before
# calling this script (see README.md). While the stack is down the gateway's
# /ops + inspect reads, ops alerting, and the events-maintenance rollup
# degrade.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

docker compose down
echo "stack stopped (volumes kept). wipe history with: docker compose down -v"
