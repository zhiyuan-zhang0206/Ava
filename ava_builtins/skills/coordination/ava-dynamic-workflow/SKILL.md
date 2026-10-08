---
name: ava-dynamic-workflow
description: "Orchestrates Ava workers with scripts. Use when a task needs parallel branching, checkpoints, and result reduction."
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

Routine result collection wakes the orchestrator at its chosen checkpoints.
Budget reminders, blockers and handoff decisions may also need a message;
workers must not hide these behind an unfinished result-file condition.

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

When script orchestration is the chosen strategy, read
[execution procedure](references/execution.md) before launching workers.
It covers exploration, scoped forks, checkpoint joins, reduction, and cleanup.

## Budget reminders and deliberate pauses

When the workflow needs budget observation or a pause, read the
[budget procedure](references/execution.md#budget-reminders-and-deliberate-pauses).
A reminder is a decision point, not permission to spend more.

## Reference scripts

| Script | Purpose |
|---|---|
| `references/gather_files.py` | Checkpoint watcher: wakes the orchestrator when the results it names have landed (all, K-of-N, or by glob) |
| `references/orchestrator_template.py` | Orchestrator skeleton — explore, fork, one checkpoint, reduce |
| `references/deep_research_orchestrator.py` | Full orchestrator: AI coding agent competitive landscape research — 7 waves, ~40 agents |
| `references/codebase_sweep_orchestrator.py` | Full orchestrator: legacy code & stale patterns sweep — 7 waves, ~28 agents |
| `scripts/deep_research_lite.py` | Scaled-down demo: 5 waves, ~11 agents — runs in persistent shell |
| `scripts/codebase_sweep_lite.py` | Scaled-down demo: 5 waves, ~11 agents — scans real codebase |

Read a reference with `ava.files.read(f"{ava.skills.ava_dynamic_workflow.path}/references/<name>.py")` (the two lite demos, runnable directly, live in `scripts/` instead — see below).

**Running the lite demos**: Each lite script is a state machine — run it once
per wave.  After spawning workers it arms that wave's checkpoint and goes idle;
the checkpoint wakes the orchestrator, and running the script again executes
the next wave.  Progress is tracked in `orchestrator_state.json`.

```python
# In a persistent shell session, run:
#   python scripts/deep_research_lite.py
# (bare `python` — the session's PATH already resolves it to this checkout's
# own venv interpreter on prod home, a dev worktree cluster, or Windows alike;
# see base/sessions/env_forwarding.py::forward_env_dict)
# Each invocation executes one wave, then idles. Repeat until "ALL DONE".
```

Each collaborator remains an ordinary persistent peer. The script assigns
roles, dispatches work and chooses checkpoints; it does not introduce a separate
worker type or workflow runtime. Peers retain their normal context and
capabilities for evaluation, recovery and handoff.

## Topology

Read [workflow topology](references/execution.md#topology) when deciding where
workers and orchestration run; do not impose extra topology on a simple task.

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
