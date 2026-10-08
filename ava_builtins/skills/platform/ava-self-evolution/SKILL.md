---
name: ava-self-evolution
description: "Mines real Ava runs for skill/plugin regressions. Use when collecting trace datasets, investigating quality trends, or proposing evidence-based fixes."
---

# Self-Evolution

Ava improves by looking at its own real usage. Every ~100 accumulated runs
(daily traffic is ~100-270, so roughly daily), the batch flow builds a **trace
dataset**, finds the runs that went badly, ties them to recently changed
skills/plugins, and proposes concrete fixes. Monday is a summary pass over the
whole week. The dataset is durable — it grows batch by batch and is the
material you iterate skills and plugins against.

This is NOT a pre-merge gate, a benchmark, or a synthetic test suite. It reads
what actually happened.

## Backpropagation analogy

Read [method context](references/method-context.md) when the reflection and
trace-mining method needs explanation; routine batch work can proceed directly.

## The batch flow

For collection, change detection, mining, and reporting, read
[batch workflow](references/batch-workflow.md). Use real recorded traces;
replaying a task is optional and never the default.

## Evaluation Loop (optimizing skill text)

When a concrete proposal warrants replay, read [evaluation loop](references/evaluation-loop.md)
and the [evaluation sub-skill](evaluation/SKILL.md). Only replay-safe read/compute
cases are eligible. Record invalid runs and isolation limits, and pair proxy
scores with task-correctness evidence.

## Data source

For dataset provenance, completeness, and empty-window failures, read the
[batch workflow's data source](references/batch-workflow.md#data-source).

## Daily threshold scan

For an assigned daily scan, read its [threshold procedure](references/batch-workflow.md#daily-threshold-scan).
A missing or broken scan is a failure to investigate, not successful inactivity.

## Cron integration

For scheduled batch/weekly work, read [cron integration](references/batch-workflow.md#cron-integration).
The current task does not itself authorize creating or changing schedules.

## Principles

- **The dataset is the deliverable.** Grow it every batch; the Monday summary
  covers the whole week. Analysis is built on it, not instead of it.
- **Read what happened; do not re-run by default.** A failure is already in the
  trace — you rarely need to reproduce it. Replay is a targeted verification
  step, not the main loop.
- **`ok` is not "good".** It only means no objective failure signal fired.
  Absolute quality of an organic run is not measured here.
- **Attribution is a suspicion.** Confirm it by reading the real trace before
  claiming a skill caused a regression.
- **Bias to a concrete fix.** A finding without a specific edit is not done.
- **Measure edits, don't guess.** In the evaluation loop, keep a skill edit
  only if the rubric score rises on the same tasks — the dataset is the judge,
  not your intuition.
