---
name: ava-dynamic-workflow
description: Builds executable scripts to dispatch persistent peers, collect results, and reduce them. Use when the user requests script orchestration, or independent work benefits from repeatable dispatch, concurrency, and aggregation; a few peers alone do not require a dynamic workflow.
---

# Dynamic Workflow

Dynamic Workflow is executable orchestration code. An ordinary persistent peer
writes a Python script that chooses units of work, dispatches peers, collects
results, and reduces them. Ava's CodeAct tools are ordinary calls in that script;
there is no separate worker type or required management tree. The agent loading
this skill can perform work, orchestrate, evaluate, or delegate those roles.

## Choose the Smallest Useful Shape

Choose direct execution, a few collaborating peers, or script orchestration
according to the work. Independent subtasks make concurrency possible, not
mandatory. A script is useful when repeated dispatch, aggregation, or recovery
justifies its setup cost. Sequential work can remain with the current peer;
goal supervision and script orchestration are independent choices.

Start with context and known capabilities. Inspect only facts that change the
next step. Use documented help or a focused probe for an uncertain API; avoid
surveying the source tree, machine, network, or all SDK modules before dispatch.
Run one representative unit through dispatch, delivery, and verification before
expanding the batch. Do not build a retry framework or dashboard to prove that
small path. If parallel execution was requested, expand after the probe.

## Execution Procedure

When script orchestration is selected, read [execution procedure](references/execution.md)
before dispatching peers. It covers durable receipts, checkpoints, verification,
and [budget reminders and deliberate pauses](references/execution.md#budget-reminders-and-deliberate-pauses).
Start with [minimal_dispatch.py](references/minimal_dispatch.py); larger examples
are illustrations, not resumable defaults.
