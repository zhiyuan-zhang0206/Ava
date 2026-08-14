---
type: doc
title: ava-ultra-speed skill — Ultra-Fast Turnaround Discipline
description: Speed disciplines for workers optimized for "extreme turnaround" (#773) — report as you go, don’t hoard until the end, never wait silently (single poll/wait no more than 15 seconds). Paired with ultra-speed-worker preset, typically pre-loaded in full via skills_to_expand_at_start, effective from spawn.
tags:
- extensions
- agent-instruction
---

# ava-ultra-speed skill — Ultra-Fast Turnaround Discipline

## What is it
A very short **speed discipline** (`$AVA_HOME/skills/ava-ultra-speed/`, #773) for workers optimized for "extreme turnaround": ① **Report as you go** — whatever the spawner/user deserves to know, say it now and keep working, don't hoard until a final wrap-up; ② **Never wait silently** — a single poll / wait never exceeds 15 seconds, better to take more short rounds than one long round in silence; ③ **Finish means end your own process** — when the mission is done, report and terminate yourself (a message resurrects you instantly with context intact); the ①② rules govern a running mission, they do not license idling after it. It exists to give "fast workers" a behavioral stance that takes effect from the very first moment of spawn.

## Deployment Form: expand-at-start
This is the canonical use case for short discipline skills that "must be effective from the start and can't be lost on compaction" — typically pre-loaded in full via `skills_to_expand_at_start` (injected as a system note, re-injected after each compact), rather than left as an index entry in `skills_to_inject_into_system_prompt` to be drilled down on demand. The paired `ultra-speed-worker` preset is exactly about pouring this skill into a worker.

## Key Dependencies
- [[ava_builtins/skills/ops_lifecycle/ops_lifecycle.ava.okf.md|Ops, Scheduling & Lifecycle Skills]] — parent functional group
- [[ava/skills.ava.okf.md|Skill System]] — `skills_to_expand_at_start` preload mechanism
- [[ava/presets.ava.okf.md|ava.agents.presets]] — `ultra-speed-worker` preset loads it into a worker
- [[ava_builtins/plugins/ava_fleet/skills/reduce-context-switch-for-human/reduce-context-switch-for-human.ava.okf.md|reduce-context-switch-for-human]] — same category of "preloaded short discipline," managing interruption rhythm (contrast: ava-ultra-speed manages turnaround speed)
