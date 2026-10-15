---
type: doc
title: ava-workflow skill — Choose how to work
description: "Strategy selection with three-way alignment, evidence-based evaluation, tracer bullets, goal definition, planning, and coordination."
tags:
- extensions
- agent-instruction
---

# ava-workflow skill — Choose how to work

## What it is

`ava-workflow` is the entry skill for choosing and adjusting how to work. Load it
for non-trivial, ambiguous, consequential, sustained, or parallel tasks. Its
selection dimensions are independent; loading it does not require an interview,
a written plan or sustained goal supervision. Parallelize independent work;
choose fork, fresh spawn or a suitable available peer by context. Short or
dependent work can execute directly with appropriate evidence.

The user supplies intent, preferences, and authority. Both human and agent
factual beliefs need calibration against evidence. Agents investigate and
choose routine methods, asking about unresolved material trade-offs in terms of
outcome, cost, speed, verification, or autonomy. Users can specify methods or
leave selection to the agent. Existing consent is not reopened at phase boundaries.

## Guides and composition

The nested guides are `capability-timescale`, `align`, `define-goal`, `plan`, and `evaluate`.
`align` owns alignment with human intent and calibration of human and agent
understanding against reality. `evaluate` owns evidence selection, human trace
review, judge calibration, comparisons, and feedback into both understanding
and the work. It separates discovery samples from measurement samples and
requires evidence beyond confidence or agreement to claim improved understanding.
These capabilities interleave; they are not sequential approval stages.

Tracer bullets are a general Workflow principle: start with a small real path
through outcome and feedback when a critical assumption or integration is
untested. Engineering and research retain their domain methods and reference
Workflow for shared alignment and evaluation. Simple tasks use direct checks.
`capability-timescale` owns evidence-based calibration when AI-dependent
feasibility, scheduling, or estimates depend on unverified capability. The entry
limits investigation to facts that change the next step; initial discussion does
not require a machine, memory, network, or SDK inventory. Deeper principles live
in `references/working-principles.md`, read for substantial implementation or
recurring bottlenecks. Discussion-only scope excludes prototype implementation.
Goal supervision, Dynamic Workflow, and watcher skills provide continuation,
executable Python orchestration, and event-driven waiting when selected. Goal
definition does not activate supervision, and dynamic orchestration does not
require goal mode or an alignment document.

Worker, reviewer, supervisor, and orchestrator are roles of persistent peers.
Context and access are chosen for each role. Budget and authority boundaries
apply to every combination. Agent persistence alone does not make scripts or
watchers recoverable.

Workflow describes responsibilities, coordination, and evidence in natural
language. Fleet task records and labels are optional collaboration conveniences,
not prerequisites. Usage reminders leave agents responsible for preserving
results, handing off, or revising remaining work; they do not force termination.

## Owners

- [Workflow](../SKILL.md) owns strategy and capability selection.
- [Align](../align/SKILL.md) owns three-way alignment, including calibration.
- [Evaluate](../evaluate/SKILL.md) owns general evaluation methods.
- [Working principles](../references/working-principles.md) owns tracer bullets.
- [[ava/agents/docs/agents.ava.okf.md|Agent interop]] owns peer lifecycle and communication.
- [[ava_builtins/skills/coordination/ava-goal/docs/ava-goal.ava.okf.md|Goal supervision]] owns sustained completion supervision.
- [[ava_builtins/skills/coordination/ava-dynamic-workflow/docs/ava-dynamic-workflow.ava.okf.md|Dynamic Workflow]] owns script orchestration guidance.
