#!/usr/bin/env bash
# Spawn sample agents with mock tasks on the preview cluster.
# Called by deploy-daily.sh after each deploy, or run manually.
#
# Spawns agents via POST /api/agents, then sends each its mock task
# via POST /api/agents/{id}/messages with {content, source}.
set -euo pipefail

CLUSTER="preview"  # display label; the preview home is fixed (CLUSTER_HOME below)
AVA_REPO="${AVA_REPO:-$HOME/Ava}"
CLUSTER_HOME="$HOME/.ava-preview"
MOCK_DIR="$AVA_REPO/scripts/preview/mock-tasks"

export PATH="$HOME/.local/bin:$PATH"

# Source cluster .env for gateway URL/port
if [ -f "$CLUSTER_HOME/.env" ]; then
    set -a; source "$CLUSTER_HOME/.env"; set +a
fi

GATEWAY_URL="http://localhost:${AVA_GATEWAY_PORT:-18000}"

echo "=== Spawning sample agents on cluster '$CLUSTER' ==="

# Ensure gateway is reachable
if ! curl -sf "${GATEWAY_URL}/api/health" >/dev/null 2>&1; then
    echo "ERROR: Gateway not reachable at ${GATEWAY_URL}"
    exit 1
fi

cd "$AVA_REPO"
.venv/bin/python3 << 'PYEOF'
import httpx, time, sys, os

BASE = os.environ.get("GATEWAY_URL", "http://localhost:18000")
MOCK_DIR = os.environ.get("MOCK_DIR", "scripts/preview/mock-tasks")

def spawn_with_task(label: str, task_rel: str):
    """Spawn an agent and send it a mock task."""
    task_path = os.path.join(os.environ.get("AVA_REPO", os.path.expanduser("~/Ava")), task_rel)
    try:
        with open(task_path) as f:
            task_content = f.read()
    except FileNotFoundError:
        print(f"  SKIP {label}: task file {task_path} not found")
        return None

    # 1. Spawn the agent
    try:
        resp = httpx.post(f"{BASE}/api/agents", json={"spawner": "preview-deploy"})
        resp.raise_for_status()
        agent_id = resp.json()["id"]
        print(f"  Spawned {label}: agent_id={agent_id}")
    except Exception as e:
        print(f"  FAIL {label}: spawn error: {e}")
        return None

    # 2. Small delay for agent process to start
    time.sleep(2)

    # 3. Send the mock task as the first message
    try:
        resp2 = httpx.post(
            f"{BASE}/api/agents/{agent_id}/messages",
            json={"content": task_content, "source": "user"},
        )
        resp2.raise_for_status()
        print(f"  → Task delivered to {label} (agent {agent_id})")
    except Exception as e:
        print(f"  → FAIL delivering task to {label}: {e}")

    return agent_id

# 1. Coding agent
print("
--- 1. Coding agent (mock PR workflow) ---")
coder_id = spawn_with_task("preview-coder", "scripts/preview/mock-tasks/mock-pr-workflow.md")

# 2. Chat agents
print("
--- 2. Chat agents (FleetView graph activity) ---")
chat_a = spawn_with_task("preview-chat-a", "scripts/preview/mock-tasks/mock-chat-exchange.md")
time.sleep(3)
chat_b = spawn_with_task("preview-chat-b", "scripts/preview/mock-tasks/mock-chat-exchange-b.md")
time.sleep(3)
chat_c = spawn_with_task("preview-chat-c", "scripts/preview/mock-tasks/mock-chat-exchange-c.md")

# 3. Notice agent
print("
--- 3. Notice agent (notice queue exercise) ---")
notice_id = spawn_with_task("preview-notices", "scripts/preview/mock-tasks/mock-notices.md")

print("
=== Sample agents spawned ===")
ids = [x for x in [coder_id, chat_a, chat_b, chat_c, notice_id] if x is not None]
print(f"Agent IDs: {ids}")
PYEOF

echo ""
echo "=== Done ==="
