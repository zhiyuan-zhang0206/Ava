#!/usr/bin/env bash
# Preview cluster validation — spawn a validation agent that runs the full suite.
set -euo pipefail

CLUSTER="preview"  # display label; the preview home is fixed ($HOME/.ava-preview)
GATEWAY_URL="${GATEWAY_URL:-http://localhost:18000}"
AVA_REPO="${AVA_REPO:-$HOME/Ava}"
SUITE_FILE="$AVA_REPO/scripts/preview/validate-tasks/suite.md"
REPORT_FILE="$AVA_REPO/preview-validation-report.md"

echo "=== Preview Cluster Validation ==="
echo "Cluster: $CLUSTER"

if ! curl -sf "$GATEWAY_URL/api/health" >/dev/null 2>&1; then
    echo "FATAL: Gateway not reachable at $GATEWAY_URL"
    exit 1
fi

echo "Spawning validation agent..."
cd "$AVA_REPO"

.venv/bin/python3 -c "
import httpx

BASE = '${GATEWAY_URL}'
with open('${SUITE_FILE}') as f:
    task = f.read()

r = httpx.post(f'{BASE}/api/agents', json={'spawner': 'preview-validation'})
r.raise_for_status()
agent_id = r.json()['id']
print(f'Validation agent: id={agent_id}')

import time; time.sleep(3)

r2 = httpx.post(
    f'{BASE}/api/agents/{agent_id}/messages',
    json={'content': task, 'source': 'user'},
)
r2.raise_for_status()
print('Task sent.')
print(f'VALIDATION_AGENT_ID={agent_id}')
"

echo ""
echo "Validation agent running (~2-5 min for full suite)."
echo "Report: $REPORT_FILE"
