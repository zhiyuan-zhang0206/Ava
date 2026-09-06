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

# The CLI owns the exact home-scoped launchd/systemd identity, including Grafana.
exec "$SCRIPT_DIR/../../.venv/bin/python" -c \
    'from cli.commands._lgtm_native import bootout_native_jobs; from shared.paths import ava_home; bootout_native_jobs(ava_home())'
