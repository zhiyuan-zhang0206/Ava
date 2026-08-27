---
name: ava-workflow
description: "Coordinates agents, the human, and reality through calibration, alignment, planning, and continuous evaluation. Use for any non-trivial task, even when the user does not explicitly ask for a workflow; large tasks need an independent adversarial review by a fresh agent."
---

# Ava Workflow — Three Actors, Three Phases, One Evaluation Thread

Work happens between **three actors** — the agents (us), the human (intent and values), and the real world (facts). Three phases keep the actors in sync, and **Evaluation threads through all of them** rather than standing at the end as a fourth step.

```
            ┌─────────────┐
            │  real world │  ← facts live here
            └──────┬──────┘
                   │  Calibrate: do our models of reality match?
        ┌──────────┴──────────┐
        │      agents         │
        └──────────┬──────────┘
                   │  Align: what do we actually want to do?
        ┌──────────┴──────────┐
        │       human         │
        └─────────────────────┘
                   │  Plan: turn the aligned intent into an executable spec
                   ▼
            work + evaluation
```

## The Three Actors and Three Phases

| Phase | Syncs | Core Question | When Needed |
|-------|-------|---------------|-------------|
| **Calibrate** (optional) | agents & human ↔ reality | "Does our mental model match the real world?" | Someone's model of the subject is off |
| **Align** | agents ↔ human | "What are we actually trying to do? What does success look like?" | Goal / constraints are fuzzy |
| **Plan** | intent → executable spec | "How do we split this into tasks agents can execute?" | Task is very large, or needs parallel execution |
| **Evaluation** (threads through all) | every phase's quality | "What counts as good? How is reality doing? When and how do we check?" | Always — the question just lives inside each phase |

### Evaluation is a thread, not a phase

Evaluation is **not the final step** — it is deeply woven into the three phases, and each phase answers a different piece of it:

- **"What counts as good?" → Align.** The success criteria you pin down in alignment *are* the evaluation standard.
- **"How is reality doing?" → Calibrate.** Calibrating is evaluating your model of the world against the world.
- **"When and how do we evaluate?" → Plan.** Checkpoints, acceptance criteria per step, and verification gates are the evaluation schedule.

The Work & Evaluate skill is this thread made explicit during execution — but it answers to all three phases, not just "after the plan." See [Work & Evaluate](work-eval/SKILL.md).

## When to Use

- **Full workflow**: open-ended, multi-step task → Calibrate (if needed) → Align → Plan (only if the task is very large or needs parallelism — see below) → Work & Eval
- **Calibrate only**: the user wants to understand part of a codebase, system, workflow, or domain — no action intent. (See the `calibrate` sub-skill.)
- **Align only**: the request is vague; you need to clarify before deciding how to proceed
- **Plan only**: requirements are already aligned, and the task is large enough (or parallel enough) to need decomposition into an executable spec
- **Work & Eval only**: you already have a clear goal and acceptance criteria and need step-by-step execution with continuous self-verification

## Plan's Scope Is Narrower Than You Think

**After Align, most work goes straight to execution — no Plan phase.** A clear goal with acceptance criteria is enough to start; running an explicit planning pass on top of alignment is overhead when the path is straightforward.

Plan earns its place in exactly two situations:

1. **The task is very large and the flow is long.** Many steps, spanning sessions or days; you need a roadmap so the work doesn't drift or get lost.
2. **Parallel execution is needed.** You must agree on the target, decompose into independent subtasks, and split them across agents — the plan is the contract that makes concurrency safe.

Everything the Plan phase used to do (decompose, estimate, mark dependencies, set checkpoints, surface risks) still lives in those two situations — it just doesn't run on every task. When in doubt, **start executing; plan when execution actually demands it.**

## The Calibration Decision (recommend, don't assume)

Calibrate exists because a plan is only as good as the model it is built on. Whether to run it is a **judgment call the agent makes and recommends** — not a checkbox the user ticks:

- **Run it** when the user's statements about the subject are vague, hedged, or contradicted by what you find; when they ask "how does X work" as part of a larger task; or when they are new to the system/domain.
- **Skip it** when the user names specific files, configs, or behaviors correctly, corrects *your* statements about the subject, or says they know it and your quick check finds nothing off.
- **Not sure? Ask.** One sentence — "are you familiar with this area?" — costs nothing and beats a wasted loop either way.
- **Calibrate per-slice, not per-task.** The user may know A well and be fuzzy on B; only run the loop where the model is uncalibrated, and skip the rest.

## The Phases Are a Network, Not a Pipeline

The phases express *logical* dependencies — understanding precedes goal-setting, goals precede decomposition — but they **overlap, interleave, and feed back into each other**. Treat them as a mesh, not a line:

- **Skip any phase** whose input is already solid (Calibrate when the user knows the subject; Align when the goal is crisp; Plan when the task is small or serial).
- **Interleave freely.** The Align interview may reveal that the user's model of some slice is wrong — switch back to Calibrate for that slice, then resume Align. Calibration may surface a fact that reshapes the goal — revisit Align. A slice of the plan that is fully settled can be executed (Work & Eval) while the rest is still being calibrated or aligned.
- **Execution feeds back.** Mid-execution, new facts can invalidate a plan premise (back to Calibrate), reveal the goal itself was off (back to Align), or demand re-splitting the remaining work (back to Plan). Work & Evaluate is where the thread reconnects to all three phases — see its skill.
- **Spawn the moment a slice is locked.** The moment any slice is calibrated *and* confirmed by the user, hand it to a sub-agent (`ava.agents.spawn`) and keep working on the remaining slices yourself — do not block the session waiting for it. Join via watcher/checkpoint per the `ava-dynamic-workflow` skill (explore → fork → join → reduce). Never spawn a slice whose premises are still being calibrated or aligned.

## Core Principles

### 1. Reality first, question second (Calibrate → Align)

Before asking the user anything, look for the answer in the environment — codebase, docs, config files, running state. Facts are discovered; only decisions need the user. When the user's model of the subject is uncalibrated, run the Calibrate loop first so the plan is grounded in reality. Then actively question — inspired by Matt Pocock's ["grill me"](https://github.com/mattpocock/skills) — working the open decisions as a design tree in rounds: each round asks the settled frontier of questions, every question carrying your recommended answer, until no branch is left silently assumed. Don't enter Plan or act until the user confirms you've reached a shared understanding.

### 2. Plan when execution demands it (Plan)

Don't start coding without a roadmap **when the task is large or parallel**. Spend time understanding the problem domain, decomposing tasks, identifying dependencies, and estimating risks. The plan doesn't need to be perfect, but it needs to exist — and it needs to exist *because the task needs it*, not because it's the default next step after Align.

### 3. Self-challenge — a weak signal (Work & Eval)

At every point — including during Calibrate and Align — challenge your own work. "Is this really moving toward the goal? Is there a simpler way? Am I solving the wrong problem?" Self-challenge is cheap and catches local slips, so keep it as the step-level loop and use it as the gate for small tasks. But it is only a weak signal: the author's own context and assumptions structurally bias their judgment. Self-review is never a substitute for independent review and cannot gate anything large or irreversible. Escalate those tasks to the independent reviewer below.

## Independent Adversarial Review

Every large task includes at least one independent adversarial review. Spawn a
fresh agent with no shared context whose only job is to hunt for faults. Never
use self-review or an agent that worked on the task, and never use `fork_from`
the executor: forking copies the context and assumptions that bias the author's
judgment.

Any one of these conditions makes a task large for this gate:

1. **Long flow** — it spans multiple sessions or days.
2. **Parallel work** — multiple agents or spawned workers produce results that join.
3. **High stakes** — the decision is irreversible or outward-facing; involves real money; affects production, data, or security; or touches a system whose failure is expensive.
4. **The user cannot review in real time** — an autonomous batch needs the review agent as a non-negotiable substitute for the user's judgment.
5. **The Plan phase produced a plan** — Plan is reserved for large or parallel work, so the independent review step belongs in that plan.

When in doubt, spawn the reviewer — a review pass costs far less than a wrong
delivery. Brief it with the alignment document, plan, and artifact locations,
not the author's narrative. The reviewer reports a verdict plus
severity-ranked P0/P1/P2 findings with evidence. Fix every P0/P1 or rebut it
with evidence, then have the same reviewer re-check only the changed parts and
rebutted findings. Repeat until the verdict is clean, and record that verdict
on the task or PR. Escalate any P0 that survives rebuttal to the user.

See [Work & Evaluate](work-eval/SKILL.md) for the full protocol. Use the
[`adversarial-review`](../../../.agents/skills/adversarial-review/) skill for
the hunting stance: assume the work is defective, prefer the failure path, and
apply severity discipline.

## Chaining Phases

When all phases are used together:

1. **Calibrate** produces a calibration record (if the subject was unfamiliar) → grounds everything downstream
2. **Align** produces an alignment document → send to user for confirmation
3. **Plan** (only when the task is large or parallel) reads the alignment document → produces an execution plan → send to user for confirmation
4. **Work & Eval** reads the execution plan (or, for smaller tasks, the alignment document directly) → executes step by step → evaluates against the success criteria from Align → delivers when done

Each confirmation point is a **checkpoint** — the user can correct course at any checkpoint, avoiding the cost of drifting too far before discovery.

## Relationship to Goal Mode

Work & Evaluate uses self-challenge as its cheap inner loop. Goal mode is a sustained supervision relationship between two agents; independent adversarial review is a fresh agent's focused fault-hunting gate.

For large tasks, keep Work & Eval inside execution and add the independent review gate above. Add goal mode too when the work needs ongoing external supervision rather than a one-shot verdict.

## Detailed Guides

- [Calibrate Phase](calibrate/SKILL.md) — the exploration + calibration loop that syncs agents' and the human's models with reality
- [Align Phase](align/SKILL.md) — how to question, when to stop, output format, and how its success criteria anchor evaluation
- [Plan Phase](plan/SKILL.md) — when to plan at all, how to decompose for concurrency, output format
- [Work & Evaluate Phase](work-eval/SKILL.md) — the evaluation thread made explicit: self-challenge execution loop that reconnects to Calibrate / Align / Plan
