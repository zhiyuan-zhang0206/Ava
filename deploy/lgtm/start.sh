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

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

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
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3003/ 2>/dev/null || true)
    [[ "$code" == "200" ]] && break
    sleep 2
done

log "stack is up:"
GRAFANA_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"
log "  Grafana      $GRAFANA_URL   (Tempo/Loki/Prometheus datasources)"
log "  sidecar OTLP http://localhost:4318    (native ava-otel-collector — all signals enter here)"
log "  Tempo intake http://localhost:14318   (sidecar traces fan-out + \`ava trace ship\` replay)"
log "  stop with: bash $SCRIPT_DIR/stop.sh"
