#!/usr/bin/env bash
#
# deploy/lgtm/start.sh — bring up the native observability backends on the
# designated LGTM host. Loki and Prometheus are native launchd jobs; Grafana
# is managed by its own host launchd job. The compose stack is retained in git
# as a rollback path only and is not started here.
#
# The gateway watchdog re-runs this script after a connection-level readiness
# failure. A backend is skipped only when its canonical launchd job is loaded
# and its endpoint is reachable.
#
set -euo pipefail
shopt -s nullglob
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

NATIVE_DIR="${AVA_HOME:-$HOME/.ava}/lgtm/native"
for name in loki prometheus; do
    if [[ ! -x "$NATIVE_DIR/bin/$name" ]]; then
        log "ERROR: native $name binary is missing; run converge / \`ava lgtm on\` first"
        exit 1
    fi
done
mkdir -p "$NATIVE_DIR/data/loki" "$NATIVE_DIR/data/prom" "$NATIVE_DIR/logs"

_reachable() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true)
    [[ -n "$code" && "$code" != "000" ]]
}

_native_plist() {
    local name="$1"
    local plists=("$HOME/Library/LaunchAgents/com.ava.$name."*.plist)
    if (( ${#plists[@]} != 1 )); then
        log "ERROR: expected exactly one slugged launchd plist for $name; found ${#plists[@]}" >&2
        for plist in "${plists[@]}"; do
            log "  $plist" >&2
        done
        return 1
    fi
    printf '%s\n' "${plists[0]}"
}

_start_native() {
    local name="$1"
    local url="$2"
    local domain="gui/$(id -u)"
    local plist
    plist="$(_native_plist "$name")"
    local label="${plist##*/}"
    label="${label%.plist}"
    if _reachable "$url" && launchctl print "$domain/$label" >/dev/null 2>&1; then
        log "$name already running — skipped"
        return
    fi
    if ! launchctl bootstrap "$domain" "$plist"; then
        log "$name launchctl bootstrap returned non-zero (already loaded is safe)"
    fi
    launchctl kickstart "$domain/$label"
    for _ in $(seq 1 15); do
        _reachable "$url" && return
        sleep 2
    done
    log "ERROR: $name did not become reachable within 30 seconds"
    exit 1
}

_start_native loki http://127.0.0.1:3100/ready
_start_native prometheus http://127.0.0.1:9090/-/ready

log "waiting for Grafana"
grafana_up=false
for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3003/ 2>/dev/null || true)
    if [[ "$code" == "200" ]]; then
        grafana_up=true
        break
    fi
    sleep 2
done
if [[ "$grafana_up" == true ]]; then
    log "Grafana is up (host-managed native launchd)"
else
    log "Grafana is not reachable yet; its host launchd job may still be starting"
fi

log "stack is up:"
GRAFANA_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"
log "  Loki         http://127.0.0.1:3100   (native launchd)"
log "  Prometheus   http://127.0.0.1:9090   (native launchd)"
log "  Grafana      $GRAFANA_URL   (native launchd; host-managed)"
log "  Tempo        remote per cluster config (AVA_TELEMETRY_TEMPO_ENDPOINT / Prometheus targets)"
log "  sidecar OTLP http://localhost:4318    (native ava-otel-collector)"
log "  stop with: bash $SCRIPT_DIR/stop.sh"
