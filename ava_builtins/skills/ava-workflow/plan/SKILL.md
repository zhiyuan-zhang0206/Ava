---
name: plan
description: "Planning phase — turn the aligned intent into an executable specification: decompose tasks, mark dependencies for concurrency, define checkpoints. Only for very large or parallel tasks — after Align, most work goes straight to execution. Can be used standalone. Load when the task is clear but large enough (or parallel enough) to need systematic decomposition."
---

# Plan — Turn Aligned Intent into an Executable Specification

Input: an alignment document (or a clear user requirement). Output: an executable plan. The granularity is "one step can be completed by one agent in one continuous session." If you're working alone, the plan is your task list; if fleet collaboration is needed, the plan is the blueprint for spawn/fork.

## When Plan Is Needed (and when it isn't)

**Plan is not the default step after Align.** A clear goal with acceptance criteria is enough to start executing directly; an explicit planning pass on top of it is overhead when the path is straightforward.

Plan earns its place in exactly two situations:

1. **The task is very large and the flow is long** — many steps spanning sessions or days, where a roadmap is needed to keep the work from drifting.
2. **Parallel execution is needed** — you must agree on the target, decompose into independent subtasks, and split them across agents. The plan is the **contract that makes concurrency safe**: each worker gets a well-defined slice with clear acceptance criteria, so parallel work joins without collision.

Everything in this skill (decompose, estimate, mark dependencies, set checkpoints, surface risks) lives inside those two situations. When in doubt, **start executing; plan when execution actually demands it.**

## Principles

1. **Understand before decomposing.** Don't skip directly to listing steps. Spend time understanding the structure of the problem domain first.
2. **Every step has clear output and acceptance criteria.** "Change the code" is not a step — "Modify module X so interface Y returns format Z, verified by unit tests" is a step.
3. **Mark dependencies and parallelizability.** Which steps can run in parallel? Which must be serial? For parallel work this is the whole point of the plan.
4. **Surface uncertainty.** Explicitly mark what you don't know — "needs investigation to determine approach" is itself a step.

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

- For every large task, include independent adversarial review as a checkpoint or step in the plan itself. It is part of the plan, not optional QA bolted on at the end.

### 5. Produce the plan document

```markdown
# Execution Plan: [Task Name]

## Overview
[A paragraph: overall approach, key technical decisions]

## Task Breakdown

| # | Task | Output | Estimate | Depends On | Risk | Priority | Parallelizable |
|---|------|--------|----------|------------|------|----------|----------------|
| 1 | ... | ... | ... | - | Low | P0 | - |
| 2 | ... | ... | ... | #1 | Medium | P0 | - |
| 3 | ... | ... | ... | #1 | Low | P1 | #2 |

## Critical Path
[The longest dependency chain, determining total timeline]

## Checkpoints
1. [After step N]: [what to verify]
2. [After step M]: [what to verify]
- [ ] Independent adversarial review: [reviewer, when, against which criteria]

## Risks & Mitigations
- Risk A (high probability / high impact): [mitigation]
- Risk B (low probability / high impact): [mitigation]

## Alternatives
[If there's a Plan B, summarize it]
```

### 6. Align and confirm

Send the plan to the user for confirmation. Highlight:
- Key decision points ("I chose approach A over B because…")
- Uncertainties ("Step 3 needs investigation to determine the approach")
- Time estimates ("Estimated total: X hours / days")

Once confirmed, enter the Work & Evaluate phase.

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
