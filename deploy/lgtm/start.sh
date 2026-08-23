#!/usr/bin/env bash
#
# deploy/lgtm/start.sh — bring up the hybrid observability backend on the
# designated LGTM host. Loki, Prometheus, and Promtail are native launchd
# jobs; Tempo and Grafana remain compose services.
#
# The gateway watchdog re-runs this script after a connection-level readiness
# failure. Native backends are probed before launchd is touched, so that path
# never restarts a backend that is already live.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

NATIVE_DIR="${AVA_HOME:-$HOME/.ava}/lgtm/native"
for name in loki prometheus promtail; do
    if [[ ! -x "$NATIVE_DIR/bin/$name" ]]; then
        log "ERROR: native $name binary is missing; run converge / \`ava lgtm on\` first"
        exit 1
    fi
done
mkdir -p "$NATIVE_DIR/data/loki" "$NATIVE_DIR/data/prom" "$NATIVE_DIR/data/positions" "$NATIVE_DIR/logs"

# Ensure the docker daemon is up. macOS can launch OrbStack itself; elsewhere
# the daemon is managed by the OS (systemd etc.) and we can only report.
if ! docker info >/dev/null 2>&1; then
    if [[ "$(uname)" == "Darwin" ]]; then
        log "docker daemon not running — starting OrbStack"
        open -a OrbStack 2>/dev/null || true
        for _ in $(seq 1 30); do
            docker info >/dev/null 2>&1 && break
            sleep 2
        done
    else
        log "docker daemon not running — start your docker daemon and rerun"
    fi
    docker info >/dev/null 2>&1 || { log "ERROR: docker daemon did not come up"; exit 1; }
fi

log "pulling images + starting Tempo and Grafana"
docker compose up -d

_reachable() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true)
    [[ -n "$code" && "$code" != "000" ]]
}

_start_native() {
    local name="$1"
    local url="$2"
    local domain="gui/$(id -u)"
    local plist="$HOME/Library/LaunchAgents/com.ava.$name.plist"
    if _reachable "$url"; then
        log "$name already running — skipped"
        return
    fi
    if ! launchctl bootstrap "$domain" "$plist"; then
        log "$name launchctl bootstrap returned non-zero (already loaded is safe)"
    fi
    launchctl kickstart "$domain/com.ava.$name"
    for _ in $(seq 1 15); do
        _reachable "$url" && return
        sleep 2
    done
    log "ERROR: $name did not become reachable within 30 seconds"
    exit 1
}

_start_native loki http://127.0.0.1:3100/ready
_start_native prometheus http://127.0.0.1:9090/-/ready
_start_native promtail http://127.0.0.1:9080/

log "waiting for Grafana"
for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3003/ 2>/dev/null || true)
    [[ "$code" == "200" ]] && break
    sleep 2
done

log "stack is up:"
GRAFANA_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"
log "  Loki         http://127.0.0.1:3100   (native launchd)"
log "  Prometheus   http://127.0.0.1:9090   (native launchd)"
log "  Promtail     http://127.0.0.1:9080   (native launchd)"
log "  Grafana      $GRAFANA_URL   (compose container)"
log "  Tempo        http://127.0.0.1:3200   (compose container; intake :14318)"
log "  sidecar OTLP http://localhost:4318    (native ava-otel-collector)"
log "  stop with: bash $SCRIPT_DIR/stop.sh"
