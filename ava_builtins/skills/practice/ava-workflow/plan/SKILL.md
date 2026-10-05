---
name: plan
description: "Turns aligned intent into an executable task graph with dependencies, concurrency, checkpoints, and done criteria. Use when work is clear but too large or parallel for one continuous session; smaller aligned tasks should proceed directly."
---

# Plan — Turn Aligned Intent into an Executable Specification

Input: an alignment document (or a clear user requirement). Output: an executable plan. The granularity is "one step can be completed by one agent in one continuous session." If you're working alone, the plan is your task list; if fleet collaboration is needed, the plan is the blueprint for spawn/fork.

For AI-dependent feasibility, scheduling, or pace assumptions, use
[capability-timescale](../capability-timescale/SKILL.md) and carry verified
evidence and uncertainty into this phase's output.

## When Plan Is Needed (and when it isn't)

**Plan is not the default step after Align.** A clear goal with acceptance criteria is enough to start executing directly; an explicit planning pass on top of it is overhead when the path is straightforward.

Plan is useful in these situations; scale its depth to the coordination needed:

1. **The task is very large and the flow is long** — many steps spanning sessions or days, where a roadmap is needed to keep the work from drifting.
2. **Parallel execution is needed** — you must agree on the target, decompose into independent subtasks, and split them across agents. Each peer needs a well-defined slice, dependencies, and acceptance criteria. A few independent tasks can use short briefs; repeated stages or large fan-outs can use an executable [dynamic workflow](../../../coordination/ava-dynamic-workflow/SKILL.md) instead of a separate plan document.

Everything in this skill (decompose, estimate, mark dependencies, set checkpoints, surface risks) lives inside those two situations. When in doubt, **start executing; plan when execution actually demands it.**

## Principles

1. **Understand before decomposing.** Don't skip directly to listing steps. Spend time understanding the structure of the problem domain first.
2. **Every step has clear output and acceptance criteria.** "Change the code" is not a step — "Modify module X so interface Y returns format Z, verified by unit tests" is a step.
3. **Mark dependencies and parallelizability.** Which steps can run in parallel? Which must be serial? For parallel work this is the whole point of the plan.
4. **Surface uncertainty.** Explicitly mark what you don't know — "needs investigation to determine approach" is itself a step.
5. **Match capabilities to the work.** Follow [Capability Matching](../SKILL.md#capability-matching); name useful skills and tools in substantial plans. Short briefs need only the capabilities their peers require.

## Process

### 1. Research existing context

Before decomposing, understand:
- Where is the relevant code? (Read `AGENTS.md`, scan project structure, search for related files)
- Are there existing designs or discussions? (Search issues, docs, memory)
- Are there similar implementations to reference?

### 2. Decompose the task

Use MECE (Mutually Exclusive, Collectively Exhaustive) decomposition:

1. **Identify sub-goals.** What independent things need to be done to reach the final goal?
2. **Order by dependency.** What must be done first? What can come later?
3. **Mark parallelism.** Which sub-goals have no dependencies between them and can proceed in parallel?
4. **Define output for each step.** A file? A PR? A deployment? A document?

### 3. Estimate & prioritize

- Mark estimated time for each step (rough is fine: minutes / hours / days)
- Mark risk level (low / medium / high)
- If time is short, mark what can be cut (P0 / P1 / P2)
- Identify the critical path — what's the longest dependency chain?

### 4. Define checkpoints

Set checkpoints at key milestones — stop at these points to verify the direction is correct, rather than pushing to the end only to discover you've drifted. **Checkpoints are the evaluation schedule**: they decide *when and how* the work gets evaluated, which is one of the three evaluation questions (the other two live in Align and Calibrate — see the connection below).

- Choose verification checkpoints that exercise the acceptance criteria. Include
  independent review when requested or useful; a plan does not make it mandatory.

### 5. Produce the plan document

```markdown
# Execution Plan: [Task Name]

## Overview
[A paragraph: overall approach, key technical decisions]

## Capabilities
- Use: [skill or MCP] — [one-line reason]
- Alternative: [only a rejected capability whose trade-off matters]
Carry forward material capability decisions from existing context; an Align phase is optional.

## Task Breakdown

Each task node names the skills and MCP tools it depends on in the `Skills / MCP` field.
Use the table for a substantial plan; a short brief or orchestration script can express the same dependencies without this template.

| # | Task | Skills / MCP | Output | Estimate | Depends On | Risk | Priority | Parallelizable |
|---|------|--------------|--------|----------|------------|------|----------|----------------|
| 1 | ... | ... | ... | ... | - | Low | P0 | - |
| 2 | ... | ... | ... | ... | #1 | Medium | P0 | - |
| 3 | ... | ... | ... | ... | #1 | Low | P1 | #2 |

## Critical Path
[The longest dependency chain, determining total timeline]

## Checkpoints
1. [After step N]: [what to verify]
2. [After step M]: [what to verify]
- [Optional additional review: what question it would answer]

## Risks & Mitigations
- Risk A (high probability / high impact): [mitigation]
- Risk B (low probability / high impact): [mitigation]

## Alternatives
[If there's a Plan B, summarize it]
```

### 6. Align and confirm

Present the plan when it helps the user steer. Ask only about material choices
not settled by existing instructions; do not request approval merely because a
plan was written. Highlight relevant open choices:
- Key decision points ("I chose approach A over B because…")
- Uncertainties ("Step 3 needs investigation to determine the approach")
- Time estimates ("Estimated total: X hours / days")

Proceed with authorized work and the chosen verification strategy; wait only
for decisions that dependent work actually needs.

## The Evaluation Connection

Planning is where **the evaluation schedule is designed**: "when and how do we evaluate" is decided here.

- **Each step's acceptance criteria** are mini-evaluation standards — the Work & Evaluate loop checks against them per step, before checking the whole against Align's success criteria.
- **Checkpoints are the evaluation gates** — planned moments where you stop and verify direction. A plan without checkpoints is a plan that defers all evaluation to the very end.
- **Dependencies and risks** tell evaluation *where to look*: the critical path and high-risk steps are the ones to evaluate most carefully, not evenly across all steps.
- Mid-execution, if evaluation shows the *decomposition* is wrong (steps are too big, wrong order, missing pieces), that's a planning failure — return here, re-split the remaining work, and update the plan. Evaluation doesn't only judge execution; it judges the plan too.

## Standalone Use

Plan can be used independently — if you already have a clear alignment document, just say "help me plan X." Even standalone, apply the two-situation test: if the task is small and serial, skip planning and execute.

## Don't

- Don't plan by default — after Align, most work goes straight to execution; plan only for very large or parallel tasks
- Don't decompose too finely — each step should be a meaningful atomic operation, not "open the file" granularity
- Don't decompose too coarsely — if a step description exceeds 3 sentences, it probably needs further breakdown
- Don't hide uncertainty — be honest about what you don't know
