---
name: plan
description: "Turns agreed work into an executable task graph. Use when dependencies, parallelism, or checkpoints need planning before execution."
---

# Plan — Make Dependencies and Checkpoints Explicit

Input: enough shared understanding to act, from a clear requirement or Align.
Output: an executable plan. Steps should have meaningful outputs and fit the
available execution context. If working alone, use a task list; if collaboration
helps, describe responsibilities, dependencies, and how peers exchange results.

For AI-dependent feasibility, approach, resource, or pace assumptions, use
[capability-timescale](../capability-timescale/SKILL.md) and carry verified
evidence and uncertainty into the plan.

## When Plan Is Needed (and when it isn't)

**Plan is not the default step after Align.** A clear goal with acceptance criteria is enough to start executing directly; an explicit planning pass on top of it is overhead when the path is straightforward.

Use a plan when the user asks for one or making dependencies, checkpoints, or
coordination explicit improves the work. Scale its depth to the need:

- **Ordered or consequential dependencies:** even a small serial task can need
  evidence, revision, or authorization before a later step.
- **Uncertainty that can change the remaining work:** identify the observation
  and decision before committing to a broad approach.
- **Duration or collaboration:** keep recovery state and handoffs for work
  spanning sessions or peers. A few tasks can use short briefs; repeated dispatch
  can use a [dynamic workflow](../../../coordination/ava-dynamic-workflow/SKILL.md).

A short sequence with its key checkpoint is often enough. Continue settled,
authorized work while planning only what dependencies or uncertainty require.

## Principles

1. **Understand before decomposing.** Don't skip directly to listing steps. Spend time understanding the structure of the problem domain first.
2. **Every step has clear output and acceptance criteria.** "Change the code" is not a step — "Modify module X so interface Y returns format Z, verified by unit tests" is a step.
3. **Mark dependencies and parallelizability.** Which steps can run in parallel? Which must be serial? For parallel work this is the whole point of the plan.
4. **Surface uncertainty.** Explicitly mark what you don't know — "needs investigation to determine approach" is itself a step.
   When an untested path controls the plan, schedule a
   [tracer bullet](../references/working-principles.md#tracer-bullets) before
   broad execution. Its output includes what we learned, not just what we built.
5. **Match capabilities to the work.** Follow [Capability Matching](../SKILL.md#capability-matching); name useful skills and tools in substantial plans. Short briefs need only the capabilities their peers require.

## Process

### 1. Research existing context

Before decomposing, find the relevant artifacts, evidence, existing decisions,
and comparable work. For repository tasks, read `AGENTS.md` and locate the
affected code. Investigate only context that can change the plan.

### 2. Decompose the task

Use MECE (Mutually Exclusive, Collectively Exhaustive) decomposition:

1. **Identify sub-goals.** What independent things need to be done to reach the final goal?
2. **Order by dependency.** What must be done first? What can come later?
3. **Mark parallelism.** Which sub-goals have no dependencies between them and can proceed in parallel?
4. **Define output for each step.** A file? A PR? A deployment? A document?

### 3. Prioritize and account for resources

- Identify consequential uncertainty and the dependencies that control useful
  completion. Prioritize work that resolves them.
- Account for limited tools, agent capacity, human review, and external waits.
  Independent work is parallelizable only within those constraints.
- Estimate durations when requested or needed for an explicit scheduling
  decision, using Capability–Timescale's evidence and uncertainty. Otherwise
  record ordering and checkpoints without invented per-step times.
- When a budget or deadline constrains the plan, name the trade-offs and what
  can change within the agreed scope.

### 4. Define checkpoints

Set checkpoints where observations could change the remaining work. Use
[Evaluate](../evaluate/SKILL.md) to choose the evidence and its interpretation;
the plan records when it will be collected and what decisions depend on it.
Evaluation can also reveal unplanned checkpoints.

- Choose verification checkpoints that exercise the acceptance criteria. Include
  independent review when requested or useful; a plan does not make it mandatory.

### 5. Keep the plan as small as useful

Use a short ordered list for a small task. The following template is for
substantial coordination; omit fields that do not help execution. Add time
estimates only under the conditions above.

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

| # | Task | Skills / MCP | Output and evidence | Depends On | Risk | Priority | Parallelizable |
|---|------|--------------|---------------------|------------|------|----------|----------------|
| 1 | ... | ... | ... | - | Low | P0 | - |
| 2 | ... | ... | ... | #1 | Medium | P0 | - |
| 3 | ... | ... | ... | #1 | Low | P1 | #2 |

## Critical Path
[Dependencies, resource limits, and waits that control completion]

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
- Resource or schedule assumptions that affect a requested commitment

Proceed with authorized work and the chosen verification strategy; wait only
for decisions that dependent work actually needs.

## The Evaluation Connection

Evaluate can run without a plan. When planning is useful, connect each important
checkpoint to a claim, observable evidence, and a possible change of direction.
Allocate attention to consequential uncertainty rather than evenly across steps.

Evidence may change the decomposition, criteria, or goal. Revise the remaining
plan and return to Align for material choices; do not force new observations
into obsolete acceptance criteria. A checkpoint need not stop independent work
or create a new approval gate.

If an outcome or stopping condition is still unclear, use
[Define Goal](../define-goal/SKILL.md). A checkpoint may test an anchored
qualitative judgment; it need not manufacture a numeric quality threshold.

## Standalone Use

Plan can be used independently with a clear request or enough shared
understanding. A small serial task may need only a few ordered steps and a
checkpoint; a clear direct action may need no separate plan.

## Don't

- Don't add a planning phase by default; use it when requested or when explicit dependencies, checkpoints, or coordination help
- Don't decompose too finely — each step should be a meaningful atomic operation, not "open the file" granularity
- Don't hide an unresolved dependency or decision inside a step that cannot be verified as a whole
- Don't hide uncertainty — be honest about what you don't know
