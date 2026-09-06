#!/usr/bin/env bash
set -euo pipefail

if [[ -f "{{AVA_HOME}}/lgtm/native/grafana/admin_password" ]]; then
    export GRAFANA_ADMIN_PASSWORD="$(<"{{AVA_HOME}}/lgtm/native/grafana/admin_password")"
fi
set -a
if [ -f "{{REPO}}/deploy/lgtm/.env" ]; then
    . "{{REPO}}/deploy/lgtm/.env"
fi
export GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-http://localhost:{{LGTM_GRAFANA_PORT}}}"
. "{{AVA_HOME}}/lgtm/native/config/runtime.env"
set +a

exec "{{AVA_HOME}}/lgtm/native/grafana-home/bin/grafana" server \
    --config "{{AVA_HOME}}/lgtm/native/config/grafana.ini" \
    --homepath "{{AVA_HOME}}/lgtm/native/grafana-home"
