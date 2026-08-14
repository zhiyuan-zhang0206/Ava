---
name: ava-dynamic-workflow
description: Write orchestrator scripts that spawn parallel workers, gather results, and reduce — the explore→fork→join→reduce pattern. Use when a task is too large for one agent, splits into independent sub-tasks, or needs parallel execution with result aggregation.
---

# Dynamic Workflow

Write long-running orchestrator scripts that decompose a complex task into
sub-tasks, farm them out to parallel worker agents, gather their results, and
synthesise a final answer — all from a single Python file.  No YAML pipelines,
no DAG configs, no external scheduler.

**Why Ava**: Ava agents are code-act agents — they execute arbitrary Python in
`execute_code`.  `ava.agents.spawn()`, `ava.watcher.launch()`, and `ava.files`
are just Python function calls.  You can write an orchestrator script, run it,
and let it create a fleet of workers, all from within your turn.  Other
frameworks require pre-declared pipelines or external orchestration services;
Ava's orchestration is a Python script.

> Inspired by Anthropic's "Building effective agents" (2024-12), specifically the
> **orchestrator-workers** and **parallelization** patterns.  This skill is the
> Ava-native realisation of those patterns.

## The pattern: explore → fork → join → reduce

```
                    ┌─────────────────────────────┐
                    │      Orchestrator Agent      │
  1. EXPLORE        │  Understand the task;        │
                    │  decide what sub-tasks exist │
  2. FORK           │  spawn worker per sub-task   │
                    └─────┬─────────┬─────────┬────┘
                          │         │         │
                    ┌─────▼──┐ ┌───▼───┐ ┌───▼─────┐
                    │Worker 1│ │Worker2│ │Worker 3 │
                    └─────┬──┘ └───┬───┘ └───┬─────┘
                          │         │         │
  3. JOIN            each writes its result file —
                     silently.  A CHECKPOINT (a watcher the
                     orchestrator launched) wakes the orchestrator once.
                          │         │         │
                          └─────────┼─────────┘
                    ┌───────────────▼─────────────┐
  4. REDUCE         │  Read the result files;      │
                    │  synthesise final answer     │
                    └──────────────────────────────┘
```

The orchestrator is woken **only at checkpoints it chose**.  Workers never
decide to wake it.

## When to use dynamic workflow

| Situation | Use dynamic workflow? |
|---|---|
| Task splits into independent sub-tasks | ✅ Yes — spawn one worker per sub-task |
| Sub-tasks don't share mutable state | ✅ Yes — each worker is isolated |
| You don't know sub-tasks ahead of time | ✅ Yes — the orchestrator (LLM) decomposes at runtime |
| Task is a single-step lookup | ❌ No — just do it yourself |
| One agent needs iterative feedback | ❌ Use `ava-goal` mode instead |
| Task must be sequential (A→B→C) | ⚠️ Chain spawn: A finishes → spawns B → spawns C |

## Procedure

### 1. Explore — understand and decompose

The orchestrator receives the user's request and decomposes it into sub-tasks.
This is an LLM reasoning step — no code yet.

```python
# Example: travel booking. The orchestrator (you) reasons:
# - Sub-task 1: Search flights SFO <-> NRT
# - Sub-task 2: Search hotels in Shinjuku
# - Sub-task 3: Curate local experiences
# These are independent -> can run in parallel
```

Sub-tasks are determined **at runtime** by the LLM, not pre-declared by a
programmer.  "Book a trip to Paris" and "book a trip to Tokyo" may need
different sub-tasks; the orchestrator adapts.

### 2. Fork — spawn workers, each ending silently

Each sub-task becomes a spawned agent.  The prompt to each worker must be
**self-contained** — all context, all data, and the completion protocol.

**Completion protocol — the same for every worker:**

1. `ava.files.write("<its result file>", <result>)`
2. `ava.self.terminate()`

Writing the file IS the handoff; ending your own process IS the completion.
**No `send_message` to the orchestrator.**  A worker that messages the
orchestrator costs it a full LLM turn; ten workers cost ten turns, nine of
which have nothing to do but wait.  Deciding when to wake up is the
orchestrator's job, and it does that with a checkpoint (step 3). A worker
never idles after its file lands — the orchestrator resurrects one (a
message brings it back with full context) only when a follow-up is needed.

```python
import ava
from pathlib import Path

handoff = Path.home() / ".ava/workspaces" / str(ava.self.AGENT_ID) / "task_handoff"
handoff.mkdir(parents=True, exist_ok=True)

flight_id = ava.agents.spawn(
    prompt=f"""You are Flight Search Worker.

Search SFO <-> NRT flights, using the following mock data:
[ ... data ... ]

When done:
1. ava.files.write("{handoff}/flights.json", <your JSON result>)
2. ava.self.terminate()
Do not message anyone — writing the file IS the handoff, and ending
yourself IS the completion.
""",
    label="flight-search-worker",
)
# ... same shape for hotels.json and activities.json
worker_ids = {"flights": flight_id}
```

**Key points**:
- Workers are spawned in rapid succession — they all start concurrently.
- The handoff directory is the bridge — workers write there, the orchestrator
  reads back.  The file's existence IS the completion signal.
- Delete the previous wave's files before spawning: a stale file reads as done.

### 3. Join — put checkpoints where you want to wake up

A **checkpoint** is a watcher you launch that messages you once when a
condition over the result files holds.  You choose how many checkpoints a
workflow has and what each one waits for:

| Shape | Checkpoint placement | When |
|---|---|---|
| **Final-only** | one watcher, after the last fork | simple workflow — nothing to decide mid-flight |
| **K checkpoints** | one per wave, where wave N+1 needs wave N's output | multi-wave workflow (2-3 checkpoints is typical) |
| **Designated reporters** | name only the results that gate the next step | wide fan-out — 10 workers, 2 of them gate, the other 8 just end |

`reference/gather_files.py` is that watcher.  Configure it by string-patching
its placeholders, and launch it BEFORE the workers start so no result is missed.

```python
watcher_code = ava.files.read(f"{ava.skills.ava_dynamic_workflow.path}/reference/gather_files.py")
watcher_code = watcher_code.replace('HANDOFF_DIR = ""', f'HANDOFF_DIR = "{handoff}"')
watcher_code = watcher_code.replace("EXPECTED_FILES: list[str] = []",
    'EXPECTED_FILES = ["flights.json", "hotels.json", "activities.json"]')
watcher_code = watcher_code.replace("ORCHESTRATOR_ID = 0",
    f"ORCHESTRATOR_ID = {ava.self.AGENT_ID}")

ava.watcher.launch(watcher_code, timeout="10m", name="gather-results")
ava.self.pause_heartbeat(600)
```

Two more placeholders shape the condition:

- `REQUIRED_COUNT = K` — wake at any K of `EXPECTED_FILES` instead of all of
  them (K-of-N).  The stragglers keep running; you reduce what landed.
- `MATCH_GLOB = "w5_*.json"` with `REQUIRED_COUNT = K` — count files by glob
  when you cannot name them at the time the checkpoint is armed.

The watcher's single message wakes you.

### 4. Reduce — synthesise the final answer

When the checkpoint wakes you, read the result files and synthesise.

```python
import json

results = {
    name: json.loads(ava.files.read(str(handoff / f"{name}.json")))
    for name in ["flights", "hotels", "activities"]
    if (handoff / f"{name}.json").exists()  # K-of-N: some may still be running
}

outbound = min(results["flights"]["outbound"], key=lambda f: f["price_usd"])
hotel = min(results["hotels"], key=lambda h: h["price_per_night_usd"])
total = outbound["price_usd"] + hotel["price_per_night_usd"] * 5

ava.ui.serve_markdown(
    f"# Itinerary\n- {outbound['airline']}: ${outbound['price_usd']}\n"
    f"- {hotel['name']}: ${hotel['price_per_night_usd']}/night\n- Total: ${total}",
    name="travel-itinerary",
)
```

### 5. Clean up

Workers that wrote their file have already ended themselves (step 2 of the
completion protocol) — a worker that wrote its file is already done.  Any
straggler you no longer need — including the ones a K-of-N checkpoint left
running — can be terminated as a fallback:

```python
for wid in worker_ids.values():
    try:
        ava.agents.terminate(wid)
    except Exception:
        pass  # already dead
```

## Reference scripts

| Script | Purpose |
|---|---|
| `reference/gather_files.py` | Checkpoint watcher: wakes the orchestrator when the results it names have landed (all, K-of-N, or by glob) |
| `reference/orchestrator_template.py` | Orchestrator skeleton — explore, fork, one checkpoint, reduce |
| `reference/deep_research_orchestrator.py` | Full orchestrator: AI coding agent competitive landscape research — 7 waves, ~40 agents |
| `reference/codebase_sweep_orchestrator.py` | Full orchestrator: legacy code & stale patterns sweep — 7 waves, ~28 agents |
| `reference/deep_research_lite.py` | Scaled-down demo: 5 waves, ~11 agents — runs in persistent shell |
| `reference/codebase_sweep_lite.py` | Scaled-down demo: 5 waves, ~11 agents — scans real codebase |

Read a reference with `ava.files.read(f"{ava.skills.ava_dynamic_workflow.path}/reference/<name>.py")`.

**Running the lite demos**: Each lite script is a state machine — run it once
per wave.  After spawning workers it arms that wave's checkpoint and goes idle;
the checkpoint wakes the orchestrator, and running the script again executes
the next wave.  Progress is tracked in `orchestrator_state.json`.

```python
# In a persistent shell session, run:
#   .venv/bin/python reference/deep_research_lite.py
# Each invocation executes one wave, then idles. Repeat until "ALL DONE".
```

## Compared to other frameworks

| | Ava dynamic workflow | LangGraph / CrewAI / etc. |
|---|---|---|
| **Orchestration defined as** | Python script | YAML / JSON DAG / Python classes |
| **Worker creation** | `ava.agents.spawn(prompt=...)` | Pre-defined agent nodes |
| **Decomposition** | Runtime LLM decision | Compile-time by programmer |
| **Communication** | Result files + orchestrator-chosen checkpoints | Framework-specific channels |
| **Parallelism** | True process-level (multi-agent) | Thread/async within one process |
| **Failure handling** | Per-worker: retry spawn; orchestrator adapts | Framework retry policies |
| **External scheduler** | None — the script IS the scheduler | Often needs separate runner service |

## Topology

The pattern composes: a worker can itself be an orchestrator — it spawns its
own sub-workers, sets its own checkpoint, reduces, and writes its result file.
Its parent sees one file, not the subtree.  Keep the tree
shallow — two levels is usually enough; every level adds latency.

## Anti-patterns

- **Every worker messaging the orchestrator when it finishes** — N workers, N
  wake-ups, N LLM turns burned on "worker 4 of 10 is done".  Workers write;
  the orchestrator wakes at its own checkpoints.
- **A checkpoint per worker** — the same cost as above, in a watcher costume.
  One checkpoint gates a whole wave.
- **Spawning workers for trivial lookups** — if the "sub-task" is a single
  `ava.web.search()` call, just do it yourself.  Spawning an agent has overhead.
- **Polling in a loop** — don't `while True: sleep(5); check()`.  Use a checkpoint.
- **Forgetting to clean up stragglers** — a worker that never wrote its file
  and never ended itself sits idle until the heartbeat nudges it; terminate
  stragglers you no longer need.
- **Over-decomposition** — 20 workers for a task that needs 3.  Each spawn is a
  real agent process with its own LLM calls.  Right-sizing the worker count is
  a **budget ↔ performance trade-off**, and where to sit on that frontier is
  the user's call — present the cost (spawns, LLM turns, wall-clock) and the
  performance gain, and let the user choose.  Never default to frugality on
  your own.
