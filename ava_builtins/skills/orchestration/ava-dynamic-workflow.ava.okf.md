---
type: doc
title: ava-dynamic-workflow skill — explore→fork→join→reduce
description: Write an orchestration script to spawn parallel workers, collect results, and reduce—explore→fork→join→reduce pattern, all in a single Python file, no YAML pipeline / DAG config / external scheduler. Use when a task is too large for one agent, can be split into independent subtasks, and requires parallel + result aggregation.
tags:
- extensions
- agent-instruction
---

# ava-dynamic-workflow skill — explore→fork→join→reduce

## What it is
Write a long-running **orchestration script**: break a complex task into subtasks, fan out to parallel worker agents, collect results, and synthesize a final answer—
all in a single Python file (`$AVA_HOME/skills/ava-dynamic-workflow/`). Its key argument (**Why Ava**): Ava
agent is a code-act agent, and `ava.agents.spawn()` / `ava.watcher.launch()` / `ava.files` are just
ordinary Python calls—you write an orchestrator script in your own turn, run it, and create a team of workers. Other frameworks
require pre-declared pipelines or external orchestration services; Ava's orchestration is just a Python script, **no YAML / DAG / external
scheduler**. (The native Ava implementation of the orchestrator-workers + parallelization pattern from Anthropic's "Building effective agents".)

## Pattern
explore (understand the task, determine what subtasks exist) → fork (spawn one worker per subtask) → join (collect results)
→ reduce (synthesize).

## Completion protocol: silent workers, orchestrator-chosen checkpoints
A worker finishes by writing its result file and then ending its own process — **it
never messages the orchestrator**. The file's existence IS the completion signal; a worker never idles after its file lands (the orchestrator resurrects one via a message only when a follow-up is needed). Waking up
is the orchestrator's decision, expressed as a **checkpoint**: a `gather_files.py` watcher the
orchestrator script launches, which sends exactly one message when the result files it names
have landed. The script decides how many checkpoints exist and what each waits for — final-only
(simple workflow), one per wave (multi-wave), or only the designated reporters whose output gates
the next step (a 10-worker fan-out can be gated by 2 files, or by a K-of-N count via
`REQUIRED_COUNT` / `MATCH_GLOB`). The banned shape is one wake-up per worker: N workers messaging
the orchestrator costs N LLM turns, N-1 of which have nothing to do.

## Key Dependencies
- [[ava_builtins/skills/orchestration/orchestration.ava.okf.md|Workflow Orchestration Skills]] — the functional group it belongs to
- [[ava/agents/agents.ava.okf.md|ava.agents]] — `spawn` fan-out; `send_message` is the checkpoint's wake-up channel, not the workers'
- [[ava_builtins/skills/ops_lifecycle/ava-watcher.ava.okf.md|ava-watcher]] — the checkpoint is a watcher
- [[ava_builtins/plugins/ava_fleet/spawn.ava.okf.md|Spawn]] — fleet-side spawn semantics (label / machine / preset)
