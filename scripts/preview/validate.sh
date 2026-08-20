#!/usr/bin/env bash
# Preview cluster validation — spawn one agent that runs validate-tasks/suite.md.
#
# Run it from the cluster's own checkout (on preview: ~/.ava-preview/source).
# The repo root comes from this script's own location and the gateway address,
# auth and home come from that checkout's cluster config, so nothing here
# hardcodes a path, a port or a secret.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "=== Preview cluster validation ($REPO_ROOT) ==="

.venv/bin/python3 - <<'PY'
import time
from pathlib import Path

from shared.http_dial import get as dial_get
from shared.http_dial import post as dial_post
from shared.machine import gateway_api_base, gateway_auth_headers
from shared.paths import ava_home

base = gateway_api_base()
# /api/agents is an authenticated route once the cluster has a secret; the
# headers are empty on a no-secret cluster, so one call site covers both.
headers = gateway_auth_headers()

health = dial_get(f"{base}/api/health", timeout=10)
if health.status_code != 200:
    raise SystemExit(f"FATAL: gateway at {base} answered /api/health with {health.status_code}")

# The agent's cwd is its own workspace, so the suite must name an absolute
# report path — one outside the checkout, so a validation run cannot dirty the
# git tree.
report = ava_home() / "preview-validation-report.md"
suite = Path("scripts/preview/validate-tasks/suite.md").read_text()
if "{{REPORT_PATH}}" not in suite:
    raise SystemExit("FATAL: suite.md lost its {{REPORT_PATH}} placeholder")
task = suite.replace("{{REPORT_PATH}}", str(report))

resp = dial_post(f"{base}/api/agents", json={"spawner": "preview-validation"}, headers=headers)
resp.raise_for_status()
agent_id = int(resp.json()["id"])
print(f"Validation agent: id={agent_id}")

time.sleep(3)  # let the spawned process come up before the first inbound
resp2 = dial_post(
    f"{base}/api/agents/{agent_id}/messages",
    json={"content": task, "source": "user"},
    headers=headers,
)
resp2.raise_for_status()
print("Task sent.")
print(f"VALIDATION_AGENT_ID={agent_id}")
print(f"Report: {report}")
PY

echo ""
echo "Validation agent running (~2-5 min for the full suite); it notifies when done."
