---
type: doc
title: ava-goal skill — Supervise another agent to achieve a goal
description: Supervise another agent across multiple rounds to achieve a goal — you are the watcher; each time the target becomes idle, you wake up, judge its latest work against the goal, and either tell it "done" or tell it "what's still missing". It is a process, not a function; built purely with existing capabilities. Only for final-state tasks that end; do not use for persistent trigger-based agents.
tags:
- extensions
- agent-instruction
---

# ava-goal skill — Supervise another agent to achieve a goal

## What it is
Track a goal across many rounds of another agent (`$AVA_HOME/skills/ava-goal/`). You are the watcher: launch a background watcher on a target agent; each time the target finishes a round and becomes idle, you wake up, judge its latest work against the goal, and either tell it done or tell it precisely what is still missing. When the goal is met, the target delivers and ends its own process — its last step is its own; terminating it is the watcher's fallback if it lingers idle. **It is a process you follow, not a function you call** — built entirely with existing capabilities (`ava.agents.spawn`/`send_message` + `ava.watcher.launch`), no special framework support, nothing to install.

## Boundaries (key design trade-offs)
Goal mode is only for **final-state tasks** (tasks that end) — here, idle means "stopped too early", so the strategy is to push it to continue. **Do not** put persistent trigger-based agents (inbox poller, daily disk check) into goal mode: their idle means "this round is done, waiting for the next trigger", pushing them is pure harassment. Persistent work is the watcher's responsibility. To quality-check one round, spawn a separate quality-check supervisor to judge **that round's** output, not a "keep going" completion driver.

## Key dependencies
- [[ava_builtins/skills/orchestration/orchestration.ava.okf.md|Workflow orchestration skill]] — belongs to functional group
- [[ava/watcher.ava.okf.md|ava.watcher]] — primitive to subscribe to target lifecycle and wake you up on idle
- [[ava_builtins/skills/ops_lifecycle/ava-watcher.ava.okf.md|watcher skill]] — persistent trigger-based tasks (explicitly not for goal mode) should use it
