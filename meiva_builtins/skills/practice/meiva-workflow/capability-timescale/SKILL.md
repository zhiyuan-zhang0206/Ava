---
name: capability-timescale
description: "Grounds AI/tool feasibility, scope, and pace in current evidence. Use before capability assumptions drive an approach, rejection, delegation, resource commitment, or estimate."
---

# Capability and Timescale Calibration

Treat current capability as a decision variable throughout the work. An old
failure can rule out something now feasible; a new model, benchmark, or larger
agent pool can create an unsupported promise. Calibrate both directions.

This is a first-class Workflow dimension, usable before choosing a goal or
approach and whenever evidence changes the plan. The system prompt's Temporal
awareness section also routes here. It does not require a planning document,
a fixed phase order, or a fresh capability survey for every task.

## Identify the Decision That Depends on Capability

Name the assumption that could change what we attempt, how we divide the work,
what resources we commit, or what pace is credible. Distinguish:

- **Demonstrated capability:** what this model/tool combination achieved, under
  which inputs, access, environment, and quality checks.
- **Transfer assumption:** what must remain true for that evidence to apply to
  this task, scale, or consequence.
- **Current bottleneck:** generation, integration, verification, rework, human
  review, external waiting, or another observed limit on useful completion.

Treat user and agent expectations as revisable claims. Neither historical human
effort nor training-time knowledge is a fixed ceiling on agent capability.
Faster generation is not a proportional reduction in end-to-end effort, and
permission to generate does not grant permission to deploy, publish, or spend.

## Gather evidence

1. Reuse recent task-relevant observations and available shared memory. Check
   dates, model/tool versions, input difficulty, environment, and what was
   actually verified. Current evidence that fits the decision can be enough.
2. Check current primary sources when release state or capability may have
   changed. Read source/configuration where relevant; search the web when needed
   and permitted. A vendor claim or benchmark suggests a hypothesis to test,
   not proof of delivered performance on this task. If evidence is unavailable,
   state the consequential uncertainty instead of inventing capability gains.
3. Where needed and authorized, run a representative
   [tracer bullet](../references/working-principles.md#tracer-bullets) through the
   actual path to an inspected result. Include the dependency whose uncertainty
   controls the decision, and distinguish demonstrated coverage from untested
   difficulty, scale, access, or concurrency.
4. Use [Evaluate](../evaluate/SKILL.md) for the probe's success evidence and
   reliability limits. Check the capacity to verify as well as to produce.
   A clean run, a confident judge, or one easy success does not establish safe
   automation or repeatability. Preserve failures and what they teach.

Investigate until the next decision is supported, not until every tool has been
surveyed. A requested discussion can specify the probe without executing it.
Choose a narrower, reversible next step when the missing evidence does not
justify a larger commitment.

## Reason About Pace and Resources

Separate elapsed time, agent/tool work, human attention, and external waits.
Use actual dependencies and the limiting resource to reason about completion.
Additional agents can shorten independent work only within tool capacity,
coordination cost, and verification/review capacity; they cannot divide a
serial critical path or eliminate an approval wait.

When estimating is requested or required by an explicit scheduling decision,
state the evidence, assumptions, range or scenarios, and dominant uncertainty.
Separate observed durations from extrapolations. Include relevant setup,
integration, review, rework, and waiting rather than reporting generation time
as delivery time. If an estimate is not defensible, identify the observation
needed to make it useful. Do not manufacture a per-step duration to fill a
template or offer an unrequested delivery promise.

Likewise, derive proposed resource use from the bottleneck. More agents,
context, or tools earn their cost only if they improve the relevant outcome.
Respect existing limits; evidence of capability does not authorize new spending.

## Use the Result

Keep the useful conclusion short: **evidence and its limits; changed capability
belief; actual bottleneck; decision affected; next check**. Revisit it when
execution contradicts an assumption or changes to models, tools, environment,
or workload could alter the decision.

- [Align](../align/SKILL.md) uses the result to calibrate human and agent
  understanding and expose newly feasible choices or mistaken expectations.
- [Define Goal](../define-goal/SKILL.md) uses it to choose a defensible outcome,
  scope, and stopping condition without silently changing the user's intent.
- [Plan](../plan/SKILL.md) uses it to choose dependencies, delegation, resource
  allocation, and checkpoints.
- [Evaluate](../evaluate/SKILL.md) determines how strongly observations support
  capability or reliability claims and supplies evidence for the next revision.

A capability gain may justify a more ambitious option; an unresolved verification
gap may justify less autonomy. Present either as a consequence of evidence,
with remaining uncertainty visible.
