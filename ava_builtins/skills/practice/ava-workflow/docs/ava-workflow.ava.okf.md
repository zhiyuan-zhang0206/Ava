---
type: doc
title: ava-workflow skill — Choose how to work
description: "Lightweight strategy selection across understanding, alignment, goal definition, continuation, peer collaboration, script orchestration, and verification."
tags:
- extensions
- agent-instruction
---

# ava-workflow skill — Choose how to work

## What it is

`ava-workflow` is the entry skill for choosing and adjusting how to work. Load it
for non-trivial, ambiguous, consequential, sustained, or parallel tasks. Its
selection dimensions are independent; loading it does not require an interview,
a written plan, sustained goal supervision, or delegation. Direct execution with
appropriate evidence is a valid strategy.

The user supplies intent, preferences, and authority. Agents discover facts and
choose routine methods, asking about unresolved material trade-offs in terms of
outcome, cost, speed, verification, or autonomy. Users can specify methods or
leave selection to the agent. Existing consent is not reopened at phase boundaries.

## Guides and composition

The nested guides are `calibrate`, `align`, `define-goal`, `plan`, and `work-eval`.
Goal supervision, Dynamic Workflow, and watcher skills provide continuation,
executable Python orchestration, and event-driven waiting when selected. Goal
definition does not activate supervision, and dynamic orchestration does not
require goal mode or an alignment document.

Worker, reviewer, supervisor, and orchestrator are roles of persistent peers.
Context and access are chosen for each role. Budget and authority boundaries
apply to every combination. Agent persistence alone does not make scripts or
watchers recoverable.

## Owners

- [Workflow](../SKILL.md) owns strategy and capability selection.
- [[ava/agents/docs/agents.ava.okf.md|Agent interop]] owns peer lifecycle and communication.
- [[ava_builtins/skills/coordination/ava-goal/docs/ava-goal.ava.okf.md|Goal supervision]] owns sustained completion supervision.
- [[ava_builtins/skills/coordination/ava-dynamic-workflow/docs/ava-dynamic-workflow.ava.okf.md|Dynamic Workflow]] owns script orchestration guidance.
