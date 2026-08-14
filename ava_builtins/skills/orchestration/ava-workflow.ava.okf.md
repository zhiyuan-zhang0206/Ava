---
type: doc
title: ava-workflow skill — Three actors, three phases, one evaluation thread
description: "A unified workflow methodology around three actors (agents, human, real world) with three phases — Calibrate (sync models with reality; optional, recommend when the user is unfamiliar with the subject, skip when known) / Align (sync intent between agents and human) / Plan (only for very large or parallel tasks: turn aligned intent into an executable spec for splitting across agents) — with Evaluation woven through all phases rather than standing as a final step (what counts as good → Align, how reality is doing → Calibrate, when and how to check → Plan). Sub-skills: calibrate / align / plan / work-eval (the evaluation thread made explicit during execution, with failures routed back to the owning phase). Load when facing any non-trivial task."
tags:
- extensions
- agent-instruction
---

# ava-workflow skill — Three actors, three phases, one evaluation thread

## What it is
A unified workflow **methodology** (`$AVA_HOME/skills/ava-workflow/`) built on three actors — **agents**, **human**,
**real world** — with three phases that keep them in sync, and **Evaluation threaded through all of them**:

- **Calibrate** (optional) syncs agents' and the human's models of the world with reality; the agent
  recommends it by the user's familiarity with the subject, and can calibrate per-slice (only the parts
  the user doesn't know).
- **Align** syncs intent between agents and human — what are we actually trying to do, what does success
  look like (this is where the evaluation standard is born: the success criteria).
- **Plan** is *not* the default step after Align — it is for **very large / long-flow tasks** and for
  **parallel execution** (agree the target, decompose into independent subtasks, split across agents); most
  work goes straight from Align to execution.
- **Evaluation is a thread, not a phase**: "what counts as good" → Align, "how is reality doing" →
  Calibrate, "when and how do we check" → Plan; `work-eval` makes the thread explicit during execution,
  with each kind of failure (step premise false / goal off / decomposition wrong) routed back to the phase
  that owns it.

The phases are a network, not a pipeline: they can chain, stand alone, interleave, feed back into each
other mid-execution, and settled slices can be handed to spawned sub-agents for parallel execution while
the session continues. Load when facing any non-trivial task (coding / research / writing / analysis / ops
changes). It nails down the soft skill of "how to do a vague task solidly" into a reusable structure.

## Four sub-skills
- **calibrate** — interactive exploration + calibration loop: the agent explores the subject (codebase,
  system, workflow, tool, domain), presents findings, follows the user's drift until everyone's mental
  model (the human's *and* the agent's) matches reality; outputs a calibration record that grounds the
  later phases. Evaluation applied to the present — the baseline every later check measures against. Use
  when the user wants to understand how something works, or as the optional first workflow phase. (Moved
  here from `.agents/skills/calibrate-understanding/`.)
- **align** — before starting, use proactive questioning to clarify the real goal, constraints, priorities
  (inspired by Matt Pocock's "grill me"); use when the task is vague, only the what is given, not the why /
  what "done" looks like. Its success criteria are the evaluation standard for everything downstream.
- **plan** — turn aligned intent into an executable specification: break down tasks, mark dependencies for
  concurrency, set checkpoints (the evaluation schedule). Only for very large or parallel tasks.
- **work-eval** — execute with **adversarial self-evaluation** at every step: did this step really push us
  closer to the goal? Is there a better way? Failures route back to Calibrate / Align / Plan rather than
  being papered over.

## Key dependencies
- [[ava_builtins/skills/orchestration/orchestration.ava.okf.md|Workflow orchestration skill]] — belongs to functional group
- [[ava_builtins/skills/orchestration/ava-dynamic-workflow.ava.okf.md|ava-dynamic-workflow]] — parallel orchestration when the task is big enough to be split among multiple workers (methodology vs mechanics); the workflow delegates "spawn the moment a slice is locked" to its explore→fork→join→reduce pattern
