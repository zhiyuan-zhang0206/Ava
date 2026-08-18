"""Full orchestrator skeleton: explore → fork → join → reduce.

Fill in the placeholders (marked with ✏️) to adapt to your task.
Run from an Ava agent's execute_code block.

Completion protocol: every worker writes its result file —
silently.  The orchestrator is woken by the ONE checkpoint armed below, not by
the workers.  For a multi-wave workflow, arm one checkpoint per wave; for a
wide fan-out, list only the results that gate the next step in CHECKPOINT_FILES
and let the other workers end unwatched.
"""

import json
from pathlib import Path

import ava

# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXPLORE — understand the task and define sub-tasks
# ═══════════════════════════════════════════════════════════════════════════════

# ✏️ Define your sub-tasks here.
# Each sub-task needs: a unique `id`, a `label` for the spawned agent,
# a `prompt` (self-contained instructions for the worker, ending with
# "write {handoff_file}"), and a result
# `file` name for the handoff.

SUB_TASKS = [
    # {
    #     "id": "subtask-1",
    #     "label": "worker-1",
    #     "prompt": "...",
    #     "file": "result_1.json",
    # },
]

# ✏️ Which results this checkpoint waits for. Empty = every sub-task's file.
# Name a subset to wake on the designated reporters only.
CHECKPOINT_FILES: list[str] = []

# ✏️ Wake as soon as this many of them exist. 0 = wait for all of them.
CHECKPOINT_COUNT = 0

# ═══════════════════════════════════════════════════════════════════════════════
# 2. FORK — spawn all workers in parallel
# ═══════════════════════════════════════════════════════════════════════════════

orchestrator_id = ava.self.AGENT_ID
handoff = Path.home() / ".ava/workspaces" / str(orchestrator_id) / "task_handoff"
handoff.mkdir(parents=True, exist_ok=True)

worker_ids: dict[str, int] = {}
all_files: list[str] = []

for task in SUB_TASKS:
    task["handoff_path"] = str(handoff / task["file"])
    all_files.append(task["file"])

    # Clean previous result — a stale file would read as "this worker is done"
    Path(task["handoff_path"]).unlink(missing_ok=True)

    wid = ava.agents.spawn(
        prompt=task["prompt"].format(handoff_file=task["handoff_path"]),
        label=task["label"],
    )
    worker_ids[task["id"]] = wid
    print(f"  spawned {task['label']}: #{wid}")

print(f"\n{len(worker_ids)} workers running in parallel")
print(f"   IDs: {list(worker_ids.values())}")

# ═══════════════════════════════════════════════════════════════════════════════
# 3. JOIN — arm the checkpoint, then idle
# ═══════════════════════════════════════════════════════════════════════════════

gating_files = CHECKPOINT_FILES or all_files

watcher_code = ava.files.read(f"{ava.skills.ava_dynamic_workflow.path}/reference/gather_files.py")
watcher_code = watcher_code.replace('HANDOFF_DIR = ""', f'HANDOFF_DIR = "{handoff}"')
watcher_code = watcher_code.replace(
    "EXPECTED_FILES: list[str] = []", f"EXPECTED_FILES = {json.dumps(gating_files)}"
)
watcher_code = watcher_code.replace("REQUIRED_COUNT = 0", f"REQUIRED_COUNT = {CHECKPOINT_COUNT}")
watcher_code = watcher_code.replace("ORCHESTRATOR_ID = 0", f"ORCHESTRATOR_ID = {orchestrator_id}")

ava.watcher.launch(watcher_code, timeout="10m", name="orchestrator-checkpoint")
ava.self.pause_heartbeat(600)

print(f"Checkpoint armed on {len(gating_files)} result file(s).")
print("   (In subsequent turns: read results from handoff, reduce, present.)")
