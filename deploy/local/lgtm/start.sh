#!/usr/bin/env bash
#
# deploy/local/lgtm/start.sh — bring up the AVA local trace viewer
# (Tempo + Loki + Prometheus + Grafana) on this machine.
#
# Zero impact on the main flow: the stack only listens on its own ports and
# writes to its own docker volumes. Stop anytime with stop.sh (data persists).
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Ensure the docker daemon is up (OrbStack on macOS).
if ! docker info >/dev/null 2>&1; then
    log "docker daemon not running — starting OrbStack"
    open -a OrbStack 2>/dev/null || true
    for i in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    docker info >/dev/null 2>&1 || { log "ERROR: docker daemon did not come up"; exit 1; }
fi

log "pulling images + starting stack"
docker compose up -d

log "waiting for Grafana"
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3003/ 2>/dev/null || true)
    [[ "$code" == "200" ]] && break
    sleep 2
done

log "viewer is up:"
GRAFANA_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"
log "  Grafana   $GRAFANA_URL   (Tempo/Loki/Prometheus datasources)"
log "  Tempo OTLP http://localhost:4318   (all signals via otel-collector)"
log "  stop with: bash $SCRIPT_DIR/stop.sh"
