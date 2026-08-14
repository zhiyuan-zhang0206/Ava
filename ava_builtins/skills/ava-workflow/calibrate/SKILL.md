---
name: calibrate
description: "Interactive exploration + calibration loop that syncs agents' and the human's mental models with reality — codebase, system, workflow, process, tool, domain — Matt Pocock grill-me style. Use when the user wants to understand how something works or asks an open-ended learning question; optional first phase of ava-workflow."
---

# Calibrate — Syncing Models with Reality

An interactive exploration + calibration loop. The human says they want to
understand X — a part of their codebase, a running system, a workflow, a
config, a tool, a domain. The agent explores whatever X is and presents
findings, the human confirms or corrects, the agent follows the drift — until
everyone's mental model matches reality. Inspired by Matt Pocock's "grill me"
pattern.

## Why this loop exists

People who say "I want to understand X" usually do not have a crisp question —
they have a fuzzy target and a partial mental model. Answering the literal
first question and stopping leaves the model uncalibrated: a wrong assumption
was never surfaced, so the human walks away confident in a wrong picture. The
loop makes the mismatch visible and lets the human steer until the model is
accurate. Calibration is the goal, not coverage.

**This phase syncs both directions:** it calibrates the *human's* model of the
world, and it calibrates *our own* model of the world (and of what the human
believes). A calibrated agent is one that knows both what is true and what the
human thinks is true — the gap between those two is exactly what this phase
closes. You are one of the two actors being calibrated; don't treat the loop
as "educating the user."

## What X can be

X is not limited to code. It can be:

- a part of a codebase — a module, a feature, a data path
- a running system — deployment topology, a data pipeline, a cron
- a workflow or process — how a request flows, how an alert reaches a human
- a configuration or integration — a tool, an API, a service mesh
- a domain concept — a business flow, a protocol, a model

Explore X on its own terms: for code read source; for systems read configs,
endpoints, logs, dashboards, and observed behavior; for processes trace the
steps end to end. Do not force a code lens on a non-code subject.

## Core loop (the agent drives this)

1. **Capture the start.** When the human says "I want to understand X",
   restate X and what they seem to already believe about it (1–3 sentences).
   This is the calibration baseline.
2. **Explore.** Find the materials that define X: for code — files, code
   paths, callers, dependencies; for systems — configs, API endpoints, logs,
   live state; for processes — the steps and their triggers. Read them, not
   just their names; trace the narrowest path from entry point to behavior.
3. **Present findings.** Report back concretely: "Here's what X looks like. Is
   this what you expected?" Anchor on the human's stated belief — confirm it,
   contradict it, or show what it is missing. Cite evidence the human can
   verify: file paths and line numbers, config keys, endpoint routes, log
   lines, observed behavior.
4. **Listen for drift.** The human may confirm, correct, or drift to Y. Drift
   is not a distraction — it is the calibration working. Never steer the
   session back to your own plan.
5. **Record the drift** in the running log (below), then explore Y and present
   findings the same way.
6. **Repeat** until the human signals they are clear ("I'm clear now", "got
   it", "that makes sense").
7. **Output the calibration record** — started at X, drifted through [Y, Z],
   ended at W, final understanding is ...

## Running drift log

Maintain a running log as the session progresses — in a scratch file or in
your notes. One row per round:

| # | Question | Finding | Human feedback | Direction change |
|---|----------|---------|----------------|------------------|
| 1 | How does the nightly report reach the user? | schedule table → gateway → telegram bridge, 3 retries | "close, but the trigger is actually…" | → Y |

Update it after every round. The log is the raw material for the calibration
record, and it is the evidence that drift was actually tracked rather than
glossed over.

## Presenting findings

- **Every question carries a recommended answer** (Matt Pocock style). Never
  ask an open "what next?" without offering a concrete pick: "I think Y is the
  most likely source of the mismatch — want me to dig there?"
- **Keep each round small.** One coherent slice of the subject, not a full
  dump. The human calibrates in steps; a wall of findings is noise.
- **Lead with contradictions.** When a finding contradicts the human's stated
  belief, say so directly and show the evidence (code path, config value, log
  line, observed behavior) — that is the highest-value moment of the loop.
- **Pause after each finding.** The goal is the human's understanding, not a
  documentation readout. End every round with an explicit check-in question.

## Design rules

- This SKILL.md is a **procedure for the agent to follow** — the agent drives
  the loop; the human only steers. Do not wait for the human to propose the
  next exploration target.
- **Never assume the endpoint** — human curiosity drives direction. There is
  no pre-planned "complete" tour to march through; follow the drift.
- **Track drift explicitly** — questions, findings, human feedback, direction
  changes. A session that never changes direction is either a perfect mental
  model or an uncurious agent; the log tells which.
- **Do not lecture.** Present findings as a check-in, not a tutorial. If the
  human already knows a slice, one sentence of confirmation and move on.
- **Calibration ends on the human's signal**, not on your checklist being
  complete.

## Output: calibration record

When the human signals they are clear, produce the calibration record in this
shape:

```
Started at: X
Drifted through: [Y, Z]
Ended at: W
Final understanding: <3-6 sentence accurate summary of how X actually works>
Corrections made: <the human's wrong assumptions that got corrected, and how>
```

Put the record in your reply, and offer to save it (a doc note, a memory
entry, or a README section) if it is worth keeping for later.

## The Evaluation Connection

Calibrate is **evaluation applied to the present**: "how is reality doing" is
the question this phase answers, and the calibration record is the answer.

- The record is the **baseline** every later evaluation measures against. When
  Work & Evaluate later asks "is this step really moving us toward the goal?",
  the "reality" side of that check comes from here — an uncalibrated baseline
  makes every later judgment untrustworthy.
- Contradictions found during calibration are the highest-value evaluation
  data in the whole workflow — they expose where a model and the world diverge.
  Lead with them, record them, and let them reshape the plan.
- Mid-execution, if a step's premise turns out false (the system doesn't do
  what the record says), that is a **calibration failure discovered during
  evaluation** — go back to this loop for that slice before continuing. The
  thread is two-way: calibrate to evaluate, and evaluate to recalibrate.

## As a phase of ava-workflow

When loaded as the optional first phase of `ava-workflow`, the calibration
record is the ground truth for the phases that follow:

- Hand the record to the Align phase. When a plan premise touches reality —
  a claim about how a system behaves, what a config controls, where a
  process starts — check it against the record, and surface any contradiction
  on the spot. A plan built on an uncalibrated model is a plan built on sand.
- The workflow's phases are a network, not a schedule: if the Align interview
  reveals the user's model of some slice is still wrong, switch back to this
  loop for that slice, then resume Align.
- Calibration is per-slice, not per-task: the user may know A well and be
  fuzzy on B. Only run the loop where the model is uncalibrated.

## Reference

- Matt Pocock's "grill me" skill pattern — a relentless, topic-agnostic
  interview that sharpens a plan or design by questioning assumptions. This
  loop adapts its question-driven calibration to understanding any subject;
  like grill-me, it is deliberately not code-only.
