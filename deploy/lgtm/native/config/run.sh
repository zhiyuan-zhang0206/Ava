#!/usr/bin/env bash
set -euo pipefail

export GRAFANA_ADMIN_PASSWORD="$(cat "{{AVA_HOME}}/lgtm/native/grafana/admin_password")"
set -a
if [ -f "{{REPO}}/deploy/lgtm/.env" ]; then
    . "{{REPO}}/deploy/lgtm/.env"
fi
. "{{AVA_HOME}}/lgtm/native/config/runtime.env"
set +a

exec "{{AVA_HOME}}/lgtm/native/grafana-home/bin/grafana" server \
    --config "{{AVA_HOME}}/lgtm/native/config/grafana.ini" \
    --homepath "{{AVA_HOME}}/lgtm/native/grafana-home"
