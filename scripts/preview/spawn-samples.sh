#!/usr/bin/env bash
# Spawn sample agents with mock tasks, to give a cluster visible activity for
# manual UI review (FleetView graph, notices, chat).
#
# Run it from the cluster's own checkout (on preview: ~/.ava-preview/source) —
# same resolution rules as validate.sh: repo root from this script's location,
# gateway address + auth from that checkout's cluster config.
#
# Each sample spawns via POST /api/agents, then sends its mock task via
# POST /api/agents/{id}/messages with {content, source}.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

echo "=== Spawning sample agents ($REPO_ROOT) ==="

.venv/bin/python3 - <<'PY'
import time
from pathlib import Path

from shared.http_dial import get as dial_get
from shared.http_dial import post as dial_post
from shared.machine import gateway_api_base, gateway_auth_headers

base = gateway_api_base()
headers = gateway_auth_headers()
mock_dir = Path("scripts/preview/mock-tasks")

health = dial_get(f"{base}/api/health", timeout=10)
if health.status_code != 200:
    raise SystemExit(f"FATAL: gateway at {base} answered /api/health with {health.status_code}")


def spawn_with_task(label: str, task_file: str) -> int:
    """Spawn an agent and send it a mock task; returns the new agent id."""
    task = (mock_dir / task_file).read_text()
    resp = dial_post(f"{base}/api/agents", json={"spawner": "preview-samples"}, headers=headers)
    resp.raise_for_status()
    agent_id = int(resp.json()["id"])
    print(f"  spawned {label}: agent_id={agent_id}")
    time.sleep(2)  # let the spawned process come up before the first inbound
    resp2 = dial_post(
        f"{base}/api/agents/{agent_id}/messages",
        json={"content": task, "source": "user"},
        headers=headers,
    )
    resp2.raise_for_status()
    print(f"  -> task delivered to {label}")
    return agent_id


print("\n--- 1. Coding agent (mock PR workflow) ---")
coder_id = spawn_with_task("samples-coder", "mock-pr-workflow.md")

print("\n--- 2. Chat agents (FleetView graph activity) ---")
chat_ids = []
for suffix, task_file in (
    ("a", "mock-chat-exchange.md"),
    ("b", "mock-chat-exchange-b.md"),
    ("c", "mock-chat-exchange-c.md"),
):
    chat_ids.append(spawn_with_task(f"samples-chat-{suffix}", task_file))
    time.sleep(3)

print("\n--- 3. Notice agent (notice queue exercise) ---")
notice_id = spawn_with_task("samples-notices", "mock-notices.md")

print("\n=== Sample agents spawned ===")
print(f"Agent IDs: {[coder_id, *chat_ids, notice_id]}")
PY
