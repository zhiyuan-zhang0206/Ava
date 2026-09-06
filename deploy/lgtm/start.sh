#!/usr/bin/env bash
#
# deploy/lgtm/start.sh — bring up the native observability backends on the
# designated LGTM host. Loki, Prometheus, and Grafana are native launchd jobs.
# The compose stack is retained in git as a rollback path only and is not
# started here.
#
# The gateway watchdog re-runs this script after a connection-level readiness
# failure. A backend is skipped only when its canonical launchd job is loaded
# and its endpoint is reachable.
#
set -euo pipefail
shopt -s nullglob
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "$(uname -s)" == Linux ]]; then
    exec "$SCRIPT_DIR/../../.venv/bin/python" -m shared.lgtm_systemd start
fi

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

NATIVE_DIR="${AVA_HOME:-$HOME/.ava}/lgtm/native"
for name in loki prometheus; do
    if [[ ! -x "$NATIVE_DIR/bin/$name" ]]; then
        log "ERROR: native $name binary is missing; run converge / \`ava lgtm on\` first"
        exit 1
    fi
done
if [[ ! -x "$NATIVE_DIR/grafana/run.sh" ]]; then
    log "ERROR: native Grafana launcher is missing; run converge / \`ava lgtm on\` first"
    exit 1
fi
mkdir -p "$NATIVE_DIR/data/loki" "$NATIVE_DIR/data/prom" "$NATIVE_DIR/logs"

# Hard gate (Task #1634): a loki.yaml change must pass the binary's own config
# validation before any start or restart. launchd restart loops a bad config
# (the 2026-08-25 wrong-field crash-loop); -verify-config catches it in
# milliseconds, so refuse to start instead of letting the job flap.
_verify_loki_config() {
    local config="$NATIVE_DIR/config/loki.yaml"
    if [[ ! -f "$config" ]]; then
        log "ERROR: native Loki config $config is missing; run converge / \`ava lgtm on\` first"
        exit 1
    fi
    if ! "$NATIVE_DIR/bin/loki" -config.file="$config" -verify-config; then
        log "ERROR: \`loki -verify-config\` rejected $config; refusing to start Loki (a bad config crash-loops the launchd job)"
        exit 1
    fi
    log "loki config verified: $config"
}

_reachable() {
    local code
    code=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true)
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

_retire_legacy_grafana() {
    local domain="gui/$(id -u)"
    local legacy_label="com.ava.grafana-native"
    local legacy_plist="$HOME/Library/LaunchAgents/$legacy_label.plist"
    if ! launchctl print "$domain/$legacy_label" >/dev/null 2>&1; then
        return
    fi
    log "retiring legacy Grafana job after native Grafana became reachable"
    if ! launchctl bootout "$domain/$legacy_label"; then
        log "WARNING: legacy Grafana job bootout returned non-zero"
    fi
    if rm -f "$legacy_plist"; then
        log "removed legacy Grafana plist $legacy_plist"
    else
        log "WARNING: could not remove legacy Grafana plist $legacy_plist"
    fi
}

# CLI/watchdog pass the resolved local bind settings. Direct shell invocation
# uses the same host/port environment defaults, never remote query URLs.
LOCAL_HOST="${AVA_LGTM_LISTEN_HOST:-127.0.0.1}"
GRAFANA_HOST="${AVA_LGTM_GRAFANA_LISTEN_HOST:-127.0.0.1}"
[[ "$LOCAL_HOST" == "0.0.0.0" ]] && LOCAL_HOST=127.0.0.1
[[ "$GRAFANA_HOST" == "0.0.0.0" ]] && GRAFANA_HOST=127.0.0.1
LOKI_URL="${AVA_NATIVE_LOKI_URL:-http://$LOCAL_HOST:${AVA_LGTM_LOKI_PORT:-3100}}"
PROM_URL="${AVA_NATIVE_PROMETHEUS_URL:-http://$LOCAL_HOST:${AVA_LGTM_PROMETHEUS_PORT:-9090}}"
GRAFANA_PROBE_URL="${AVA_NATIVE_GRAFANA_URL:-http://$GRAFANA_HOST:${AVA_LGTM_GRAFANA_PORT:-3003}}"

_verify_loki_config
_start_native loki "$LOKI_URL/ready"
_start_native prometheus "$PROM_URL/-/ready"
_start_native grafana "$GRAFANA_PROBE_URL/"
_retire_legacy_grafana

grafana_password="$NATIVE_DIR/grafana/admin_password"
if [[ -f "$grafana_password" ]]; then
    alert_rules=$(curl --noproxy '*' -s -u "admin:$(<"$grafana_password")" \
        "$GRAFANA_PROBE_URL/api/v1/provisioning/alert-rules" 2>/dev/null || true)
    if alert_count=$(printf '%s' "$alert_rules" | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))' 2>/dev/null); then
        if [[ "$alert_count" -lt 18 ]]; then
            log "WARNING: Grafana provisioned $alert_count alert rules; expected at least 18"
        fi
    else
        log "WARNING: Grafana alert-rule readiness response was not valid JSON"
    fi
else
    log "WARNING: Grafana admin password is unavailable; skipped alert-rule readiness check"
fi

log "stack is up:"
GRAFANA_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"
log "  Loki         $LOKI_URL   (native launchd)"
log "  Prometheus   $PROM_URL   (native launchd)"
log "  Grafana      $GRAFANA_URL   (native launchd)"
log "  Tempo        remote per cluster config (intake AVA_TELEMETRY_TEMPO_ENDPOINT; query AVA_TELEMETRY_TEMPO_QUERY_URL)"
log "  sidecar OTLP ${AVA_TELEMETRY_OTLP_ENDPOINT:-http://localhost:4318}   (native ava-otel-collector)"
log "  stop with: bash $SCRIPT_DIR/stop.sh"
