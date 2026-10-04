#!/usr/bin/env bash
set -euo pipefail

if [[ -f "{{AVA_HOME}}/lgtm/native/grafana/admin_password" ]]; then
    export GRAFANA_ADMIN_PASSWORD="$(<"{{AVA_HOME}}/lgtm/native/grafana/admin_password")"
fi
# The direct Telegram contact point (contact.yml) reads these; a host without a
# bot gets inert placeholders so provisioning stays valid (the send just fails
# at the Bot API and the webhook route still delivers).
if [[ -f "{{AVA_HOME}}/lgtm/native/grafana/telegram.env" ]]; then
    . "{{AVA_HOME}}/lgtm/native/grafana/telegram.env"
fi
export AVA_ALERTS_TELEGRAM_BOT_TOKEN="${AVA_ALERTS_TELEGRAM_BOT_TOKEN:-unconfigured}"
export AVA_ALERTS_TELEGRAM_CHAT_ID="${AVA_ALERTS_TELEGRAM_CHAT_ID:-0}"
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
