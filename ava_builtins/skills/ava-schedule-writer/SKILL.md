---
name: ava-schedule-writer
description: Turn a natural-language scheduling need into a gateway-hosted schedule — clarify the trigger, write a resumable script, and create it via /api/schedules.
---

# Schedule Writer

A **schedule** is a persistent session the gateway supervises: a script it runs in
a session and restarts if it crashes (with a circuit breaker). You turn a
user's request into one — you clarify what they want, write the script, and create
it through the API. The script is arbitrary Python: whatever `if` / `for` /
threshold logic the trigger needs, you write it directly (there is no schedule
DSL).

## 0. Built-in schedules already exist

Ava provisions a set of built-in schedules on every gateway boot from
`schedules/manifest.json` (product schedules — self-evolution, memory —
enabled; cluster-operator schedules — e.g. trace-ship-tempo — disabled). Do
not create a schedule whose name is in the manifest: the API refuses it (409).
Extend the manifest + template instead if the need is a general Ava capability;
write a user-specific schedule (like quant-daily-check) via the normal flow.

## 1. Clarify (ask before writing)

Ask the user three things, then write to their answers:

1. **Trigger** — time, event, or both?
   - *time*: a wall-clock cadence ("every night at 3am", "weekdays at 9").
   - *event/threshold*: a condition over cluster state ("when the memory pool grows
     >500 lines in 2h", "when the repo burns >200k tokens in an hour").
   - *both*: fire on either (a nightly safety net **or** a threshold — the common
     shape for consolidation/ava-self-evolution work).
2. **Skip** — any fire it should skip? ("don't run if no new memory was written",
   "skip weekends", "only during work hours").
3. **On error** — if the work fails: **surface** it (let it raise — the runner
   records the traceback to the schedule's `last_error`, shown on the manage
   page), **retry** (catch, `print` the error so it lands in the schedule's logs,
   and let the next loop try again), or **ignore** (catch and carry on)?

## 2. Write the script (the rules that matter)

**The script must be resumable.** The gateway can kill it at any instant (restart,
your edit, a crash-relaunch). So structure it **compute-next → sleep → act →
loop**, recomputing the next fire from the wall clock every iteration. **Never act
on startup** and never rely on an in-memory counter — both re-fire on every
restart. A resumable loop naturally skips a fire missed during downtime.

**Time** — use `next_fire`, and pass the **cluster** timezone, not a literal zone
name: a hard-coded zone pins one deployment's clock onto every cluster that ever
runs the schedule (a 04:00 maintenance window becomes 13:00 mid-peak nine hours
west, and a weekly cron lands on the wrong calendar day). `next_fire` with
`timezone=None` computes in UTC, which is the right choice only for a pure
period like `*/5 * * * *`.

```python
from datetime import UTC, datetime
from shared.config import settings
from shared.watcher import next_fire
nxt = next_fire("0 3 * * *", after=datetime.now(UTC), timezone=settings.general.timezone)
```

**Event / threshold** — read the cluster's own state. All events live in the
unified `events` table (`ts / trace_id / agent_id / machine / process /
category / kind / level / source / attributes`; `category` = audit |
telemetry | log). The legacy `agent_events` / `event_log` tables are write
mirrors — never query them:
- tokens: `events` `event_name='llm_usage'`, sum `attributes->>'in_total' + out_total`.
- agents spawned: `events` `category='audit' AND event_name='spawn'` count.
- memory growth: `ava.shell.run("cd ~/.ava/memory && git diff --numstat | awk '{s+=$1+$2} END{print s+0}'")`.
- Query the DB with `ava.DB` (works from a schedule — no agent identity needed).

**Compound** is just `if a or b:`. **Skip** is `if should_skip(): continue`.

**Reuse an agent, don't re-spawn it.** If the task runs an agent (a consolidator, a
monitor), reuse the one with its label — resurrect it if terminated, wake it if
idle — and only spawn when none exists. State lives in the agents table (the label
is the identity); the schedule keeps none:

```python
import ava
from ava.agents import AgentStatus as S

def ensure_agent(label: str, prompt: str) -> int:
    mine = [a for a in ava.agents.list_agents(filter_by_status=(
                S.RUNNING, S.IDLING, S.TERMINATED, S.ALLOCATED, S.STARTING, S.RESTARTING))
            if a.label == label]
    if mine:
        a = max(mine, key=lambda r: r.agent_id)   # label is not unique -> newest
        if a.status == S.TERMINATED:
            ava.agents.resurrect(a.agent_id, prompt)
        else:
            ava.agents.send_message(a.agent_id, prompt)   # idle wakes, running enqueues
        return a.agent_id
    return ava.agents.spawn(prompt=prompt, label=label)
```

**Actor caveat.** A schedule runs as `schedule:<id>`, not an agent. So
`ava.agents.*`, `ava.DB`, `ava.shell`, and `ava.memory` all work, but the calls
that need an agent identity — `ava.self.*`, `ava.watcher.*`, `ava.ui.*` — do
not. To surface something to the user, either let the schedule raise (recorded to
`last_error`) or hand the work to an agent via `ensure_agent(...)` and let *it*
notify.

## 3. Full example — nightly-or-threshold, reuse-by-label

```python
# consolidate the memory pool nightly at 3am, or whenever it grows >500 lines in 2h
import time
from datetime import UTC, datetime
import ava
from ava.agents import AgentStatus as S
from shared.config import settings
from shared.watcher import next_fire

def ensure_agent(label, prompt):
    mine = [a for a in ava.agents.list_agents(filter_by_status=(
                S.RUNNING, S.IDLING, S.TERMINATED, S.ALLOCATED, S.STARTING, S.RESTARTING))
            if a.label == label]
    if mine:
        a = max(mine, key=lambda r: r.agent_id)
        (ava.agents.resurrect if a.status == S.TERMINATED else ava.agents.send_message)(a.agent_id, prompt)
        return a.agent_id
    return ava.agents.spawn(prompt=prompt, label=label)

def pool_growth():
    out = ava.shell.run("cd ~/.ava/memory && git diff --numstat | awk '{s+=$1+$2} END{print s+0}'")
    return int((out or "0").strip() or 0)

def consolidate(reason):
    try:
        ensure_agent("memory-arbiter",
                     f"Consolidate the memory pool now ({reason}). Follow ava.skills.ava_memory.consolidation.")
    except Exception as e:              # on-error: log it and carry on (per the user's choice)
        print(f"consolidate failed ({reason}): {e}")   # lands in the schedule's logs

while True:                             # resumable: recomputes from the clock each loop
    nxt = next_fire("0 3 * * *", after=datetime.now(UTC), timezone=settings.general.timezone)
    fired = False
    while datetime.now(UTC) < nxt:
        if pool_growth() >= 500:
            consolidate("pool grew >=500 lines"); fired = True
            time.sleep(2 * 3600)        # debounce the same growth
            break
        time.sleep(60)
    if not fired:
        consolidate("nightly 03:00")
```

## 4. Create it (POST /api/schedules)

Show the user the script and the readback ("I'll wake the memory-arbiter agent
nightly at 3am PT or when the pool grows ≥500 lines in 2h — reusing the same agent,
not spawning a new one"). On their confirmation, create it — the gateway starts
supervising it immediately:

```python
import os, httpx, pathlib
base = os.environ.get("AVA_GATEWAY_URL", "http://localhost:8000")
body = {
    "name": "memory-arbiter",             # unique; also the schedule's handle
    "description": "<the user's request, verbatim>",
    "script": pathlib.Path("schedule.py").read_text(),
    "command": "python schedule.py",      # default; how the runner runs the script
}
r = httpx.post(f"{base}/api/schedules", json=body,
               headers={"Authorization": f"Bearer {os.environ['AVA_CLUSTER_SECRET']}"})
print(r.status_code, r.text)             # 201 on success; 400 = script syntax error; 409 = name taken
```

The API `compile()`-checks the script (400 with the offending line) and refuses a
duplicate name (409). After creating, tell the user it is live and how to manage it
(the `/control/schedules` page: status, logs, edit, start/stop).

## Editing later

To change a schedule, `PUT /api/schedules/{id}` with the new `script` (or any
field). The change is snapshotted for roll-back, and an enabled schedule reloads
onto the new script at once. `start` / `stop` / `restart` are `POST
/api/schedules/{id}/{action}`.

Same surface from a shell, if that is where you already are — `ava schedules`
covers every route above (`ls` / `get` / `create` / `update` / `delete` /
`start` / `stop` / `restart` / `logs` / `runs`), takes a name or an id, and reads
the script from `--script-file` (`-` = stdin). Use it for inspection and
one-off control; keep the httpx form above for the create step, where you are
already holding the script in the same process.
