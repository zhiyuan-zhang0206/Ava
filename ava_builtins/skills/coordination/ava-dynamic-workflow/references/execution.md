# Dynamic workflow execution

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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
from base.paths import workspace_dir

handoff = workspace_dir(ava.self.AGENT_ID) / "task_handoff"
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

`references/gather_files.py` is that watcher.  Configure it by string-patching
its placeholders, and launch it BEFORE the workers start so no result is missed.

```python
watcher_code = ava.files.read(f"{ava.skills.ava_dynamic_workflow.path}/references/gather_files.py")
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

The watcher's single message wakes you; delivery retries across a gateway /
agent restart window, and if every attempt fails the watcher exits 2, so the
loss surfaces in its exit notice.

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

page_dir = handoff / "travel-itinerary-page"
page_dir.mkdir(exist_ok=True)
(page_dir / "index.html").write_text(
    f"<!doctype html><html><body><h1>Itinerary</h1><ul>"
    f"<li>{outbound['airline']}: ${outbound['price_usd']}</li>"
    f"<li>{hotel['name']}: ${hotel['price_per_night_usd']}/night</li>"
    f"</ul><p>Total: ${total}</p></body></html>"
)
ava.ui.serve(str(page_dir), name="travel-itinerary", port=<free port>)
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

## Budget reminders and deliberate pauses

When budget observation is useful, choose the IDs, birth lineage, time window,
thresholds and reminder recipients before dispatch, using `ava-being-a-long-running-agent`'s usage
script. Reuse agreed limits; do not invent spending authority. Keep the report
scope and watcher session ID with the run notes. A reminder asks
for a decision; it neither terminates peers nor grants more spending.

If the responsible agent chooses to pause, persist that decision before the next
wave or batch dispatch. Collect available results, label partial artifacts as
partial, and record unfinished units, peer IDs, checkpoints, observation scope
and a concrete resume condition. Ask active peers to finish a safe unit and
preserve their own handoff; do not hard-kill them just because a threshold fired.
A worker that cannot complete its result must report that disposition rather
than making a partial file look like a finished answer.

On every checkpoint, heartbeat, script re-entry or restart, read the run notes
before dispatching. A late result or watcher exit does not clear a pause. Update
the inventory of preserved artifacts without automatically spawning replacements
or advancing the next wave. Resume only when the recorded condition is met,
using the existing IDs and completed results. Stop unneeded checkpoint watchers
by their recorded session IDs when appropriate; late notices still require this
same check. These notes belong to the generated script and peers' working files,
not a new core workflow or budget controller.

## Topology

The pattern composes: a worker can itself be an orchestrator — it spawns its
own sub-workers, sets its own checkpoint, reduces, and writes its result file.
Its parent sees one file, not the subtree.  Keep the tree
shallow — two levels is usually enough; every level adds latency.
