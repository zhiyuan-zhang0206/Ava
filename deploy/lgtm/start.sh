#!/usr/bin/env bash
#
# deploy/lgtm/start.sh — bring up the cluster's observability backend
# (Tempo + Loki + Prometheus + Grafana) on the designated LGTM host.
#
# Idempotent (`docker compose up -d`); run by converge on the host carrying
# the $AVA_HOME/lgtm-host marker and re-run by the gateway watchdog's
# healthcheck when a readiness probe hits a connection failure (see
# services/healthchecks/lgtm.py). The stack is a live dependency of the
# gateway's /ops + inspect endpoints while it serves — see README.md.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# The lifecycle derives this from AVA_GATEWAY_URL + /grafana/. Refuse to
# resurrect the old direct :3003 browser entry when start.sh is invoked by hand.
: "${GRAFANA_ROOT_URL:?GRAFANA_ROOT_URL must be the gateway URL ending in /grafana/}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
PYTHON_BIN="${AVA_LGTM_PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    log "ERROR: Python is required to validate Grafana's authenticated identity" >&2
    exit 1
}

grafana_viewer_ready() {
    local user_json orgs_json search_json
    user_json=$(curl --noproxy '*' -fsS \
        -H 'X-Ava-Grafana-User: ava-cluster-viewer' \
        -H 'X-Ava-Grafana-Role: Viewer' \
        http://127.0.0.1:3003/grafana/api/user 2>/dev/null) || return 1
    printf '%s' "$user_json" | "$PYTHON_BIN" -c '
import json, sys
user = json.load(sys.stdin)
valid = (
    isinstance(user, dict)
    and user.get("login") == "ava-cluster-viewer"
    and user.get("isGrafanaAdmin") is False
)
raise SystemExit(0 if valid else 1)
' || return 1

    orgs_json=$(curl --noproxy '*' -fsS \
        -H 'X-Ava-Grafana-User: ava-cluster-viewer' \
        -H 'X-Ava-Grafana-Role: Viewer' \
        http://127.0.0.1:3003/grafana/api/user/orgs 2>/dev/null) || return 1
    printf '%s' "$orgs_json" | "$PYTHON_BIN" -c '
import json, sys
orgs = json.load(sys.stdin)
valid = isinstance(orgs, list) and bool(orgs) and all(
    isinstance(org, dict) and org.get("role") == "Viewer" for org in orgs
)
raise SystemExit(0 if valid else 1)
' || return 1

    search_json=$(curl --noproxy '*' -fsS \
        -H 'X-Ava-Grafana-User: ava-cluster-viewer' \
        -H 'X-Ava-Grafana-Role: Viewer' \
        'http://127.0.0.1:3003/grafana/api/search?limit=1' 2>/dev/null) || return 1
    printf '%s' "$search_json" | "$PYTHON_BIN" -c '
import json, sys
raise SystemExit(0 if isinstance(json.load(sys.stdin), list) else 1)
'
}

# Ensure the docker daemon is up. macOS can launch OrbStack itself; elsewhere
# the daemon is managed by the OS (systemd etc.) and we can only report.
if ! docker info >/dev/null 2>&1; then
    if [[ "$(uname)" == "Darwin" ]]; then
        log "docker daemon not running — starting OrbStack"
        open -a OrbStack 2>/dev/null || true
        for i in $(seq 1 30); do
            docker info >/dev/null 2>&1 && break
            sleep 2
        done
    else
        log "docker daemon not running — start your docker daemon and rerun"
    fi
    docker info >/dev/null 2>&1 || { log "ERROR: docker daemon did not come up"; exit 1; }
fi

log "pulling images + starting stack"
docker compose up -d

log "waiting for Grafana"
grafana_ready=false
for i in $(seq 1 30); do
    if grafana_viewer_ready; then
        grafana_ready=true
        break
    fi
    sleep 2
done
if [[ "$grafana_ready" != "true" ]]; then
    log "ERROR: authenticated Grafana Viewer readiness failed" >&2
    exit 1
fi

log "stack is up:"
log "  Grafana      $GRAFANA_ROOT_URL   (authenticated gateway Viewer)"
log "  sidecar OTLP http://localhost:4318    (native ava-otel-collector — all signals enter here)"
log "  Tempo intake http://localhost:14318   (sidecar traces fan-out + \`ava trace ship\` replay)"
log "  stop with: bash $SCRIPT_DIR/stop.sh"
