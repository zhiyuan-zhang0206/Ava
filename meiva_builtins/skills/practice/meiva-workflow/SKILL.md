---
name: meiva-workflow
description: "Selects a working strategy when capability, feasibility, shared understanding, evaluation, planning, or coordination needs deliberate attention."
---

# Meiva Workflow — Choose How to Work

Workflow is the lightweight entry point for choosing a working strategy. Align
the agent with human intent, and both human and agent understanding with reality.
The human supplies preferences and authority; neither participant's factual
beliefs are automatically correct. Calibrate current capability and timescale
before they determine what to attempt, rule out, or promise. Evaluate observations
to improve both shared understanding and the work itself. Loading this skill does not require
an interview, a plan document, or goal supervision. Direct execution with a
suitable check fits short or dependent work; parallelize independent work.

## Choose a Working Strategy

First assess ambiguity, scale, consequences, duration, and independent work.
These are separate dimensions: a small consequential action can need alignment,
while a large repetitive migration can need script orchestration without an
interview. Respect the user's explicit choices and existing authorization.

| Dimension | Available choices | Selection question |
|---|---|---|
| Alignment | Existing context, targeted reconnaissance, brief clarification, deeper Align | Which gap in intent, human understanding, or agent understanding changes the next action? |
| Capability and timescale | Reuse current evidence, investigate a changed capability, run a representative probe | What is feasible now, what limits reliable completion, and which scope, method, or estimate does that change? |
| Goal definition | Use the stated outcome, sharpen it briefly, Define Goal | Is completion clear and verifiable? |
| Planning | Execute directly, keep a short dependency plan, plan substantial coordination | Which dependencies or checkpoints need to be explicit, even for small serial work? |
| Continuation | Finish this turn, sustained goal supervision, event-driven waiting | What should start the next useful action, and what should stop it? |
| Collaboration | Work alone, reuse or spawn a few peers, Dynamic Workflow | Does delegation or a reusable script improve this task? |
| Evaluation | Direct checks, trace review, controlled comparisons, calibrated judges, independent review | What observation would change our belief or decision, and how trustworthy is it? |

Choose each dimension independently. Goal definition does not enable goal mode;
goal mode does not require an Align session or a dynamic workflow. A dynamic
workflow is executable orchestration code, not merely several delegated tasks.
Load the relevant detailed guide only when that capability is needed.

Examples:
- A clear bug fix: execute directly and reproduce the failing-then-passing case.
- A clear outcome needing repeated work: use it as the goal and ask a normal peer
  to pursue it under sustained supervision, without a Define Goal interview.
- A large batch with known checks: run a dynamic workflow without goal mode.
- A vague consequential request: Align, then choose the relevant domain skills.
- An already settled change: skip Align and strengthen independent evaluation.
- A task dismissed using old AI limitations: check current capability before
  ruling it out; a recent benchmark also needs task-relevant verification before
  it supports a commitment.

## Calibrate Capability and Timescale

[Capability–Timescale](capability-timescale/SKILL.md) is a first-class working
dimension. Use it when an AI/tool capability assumption can change the goal,
feasibility, approach, delegation, resource commitment, or schedule. Revisit it
when new evidence changes the bottleneck; it is not only an estimation step
inside Plan.

Calibration works in both directions: stale assumptions can hide opportunities,
while faster generation or more agents can conceal verification, rework, human
attention, integration, and external-wait bottlenecks. Use current task evidence
and a small real probe where needed. Feed the result into Align's understanding,
the goal's scope, and Plan's dependencies; use Evaluate to establish what the
probe actually demonstrates. Match investigation to the decision, reusing
verified capabilities for routine work instead of repeatedly surveying tools.

## Recommend Before Asking

Choose routine methods within the authorized scope. Tell the user the strategy
briefly when it helps them steer; do not ask them to understand or select between
internal names such as goal mode, Define Goal, or Dynamic Workflow. Ask only when
an unsettled choice materially changes outcome, cost, speed, verification,
autonomy, or authority. Give a recommendation and explain the concrete trade-off.
For example: "I recommend parallel investigation followed by independent checks;
this is faster but costs more than a sequential pass."

Users may explicitly choose any method, or delegate the choice. Existing consent
remains valid; loading a skill or producing a plan does not create another
approval gate. A chosen method never grants additional spending or permissions.

## Investigate Only What Changes the Next Step

Start with the context and capability index already available. Identify which
unknown fact would change the next action; investigate that fact and proceed.
Do not inventory the machine, all SDK modules, installed packages, memory, or
network routes merely because this skill was loaded. Discover relevant facts
before asking the human, but ask human-only choices directly rather than
searching the environment for them.

For an initial discussion, clarify the outcome and material choices first.
Check execution prerequisites when choosing or attempting the relevant action.
For a clear local task, reproduce the issue and verify the result. When an
untested path or assumption controls the work, use a
[tracer bullet](references/working-principles.md#tracer-bullets): the smallest
real path from input through outcome to feedback. This applies to research,
content, operations, and software. A checked simple answer already closes that
loop; do not add a separate pilot to every task.

## Delegation decisions

Proactively parallelize independent work. Fork when your existing context is
valuable to the task; otherwise spawn fresh or reuse a suitable, available agent.
Keep short or dependent steps with the current agent. Choose tools by demonstrated
capability for the task, not fixed file-count thresholds.
For Meiva's external-worker launch, supervision, resume, or takeover mechanics,
load the external-agents guide in Meiva Guide.

## Capability Matching

1. Identify the current gap: domain knowledge, execution access, verification,
   coordination, or recovery. Start from the task, not a skill's name.
2. Scan skill descriptions and available tools; read candidates whose trigger
   and expected output cover that gap. Check current repository and environment
   facts needed for the next action before assuming a capability is usable.
3. Distinguish reference material from an executable procedure. Reading a
   migration skill does not authorize a migration. Check tools, environment,
   permissions, and cost prerequisites before using a procedure.
4. Select the smallest adequate combination. Reuse existing peers when suitable;
   spawn peers or write orchestration code when that improves the result.
5. Revisit selection when evidence reveals a new gap or invalidates a premise.

Capability matching selects a candidate tool or skill. Capability–Timescale
checks whether its demonstrated performance supports this task and commitment;
a catalog description alone cannot establish feasibility or throughput.

For substantial Align or Plan outputs, name selected skills and tools with a
short reason. Mention a rejected alternative only when its trade-off matters.
A routine task needs no capability report or mandatory `Not used` entry.

## Keep the Strategy Proportional and Adjustable

Use a sentence or a few working notes for ordinary tasks. Persist the objective,
acceptance evidence, boundaries, next action, and necessary coordination state
when work spans turns or needs recovery. Use durable working notes or existing work records; no collaboration plugin,
task registry, label convention, or specific SDK is required.

Alignment, capability/timescale calibration, goal definition, planning, and
evaluation can stand alone, interleave,
and feed back into one another. Execute settled slices while investigating
others when safe; reassess independent work for parallel execution at useful
checkpoints. Changes within the agreed scope need no ceremonial reapproval;
changes to outcome, authority, or authorized spending need the relevant decision.

## Optional Methods, Explicit Commitments

A request to discuss, inspect, or evaluate does not authorize implementation or
publication. Treat prototypes and demonstration pages as implementation too;
do not rename them to bypass an explicit "do not write code" instruction.
Explicit exclusions such as "no external systems" remain boundaries; recommend
an alternative or explain a limitation without interpreting the exclusion away.


Skipping Define Goal still requires a recognizable outcome and completion
condition when pursuing a sustained goal. Skipping Align cannot settle an open
permission question. Evaluation depth is selectable, but completion claims need
supporting evidence. Budget and authority boundaries apply to every combination;
this skill does not enforce a hard spending ceiling. When a usage reminder
arrives, reassess the remaining work and budget: preserve results, prepare a
handoff, narrow the next step, or ask for a changed budget as appropriate. A
reminder does not automatically terminate peers or discard in-flight work.

All collaborators are persistent peer agents. Worker, supervisor, reviewer, and
orchestrator are roles assigned through context and configuration, not separate
agent types. Choose fresh context or a fork and appropriate access for the role;
peer identity does not require identical permissions or copied assumptions.
Agent persistence does not automatically make an orchestration script resumable:
record progress and account for interrupted watchers and external effects.

## Deeper Working Principles

For an untested end-to-end path, recurring bottlenecks, substantial implementation,
or work that must survive interruptions, read
[working-principles](references/working-principles.md).
It covers tracer bullets, investing in future work, causal investigation, and
presenting decisions. A simple question or initial requirements discussion does
not need that full procedure.

## Detailed Guides

- [Align](align/SKILL.md) — align intent and calibrate human and agent understanding against reality.
- [Capability–Timescale](capability-timescale/SKILL.md) — ground feasibility, scope, approach, and pace in current task evidence and actual bottlenecks.
- [Define Goal](define-goal/SKILL.md) — sharpen an unclear outcome and its evidence.
- [Plan](plan/SKILL.md) — record dependencies and checkpoints when useful.
- [Evaluate](evaluate/SKILL.md) — discover failures, calibrate judgment, compare alternatives, and improve the work with limited human attention.
- [Goal pursuit](../../coordination/meiva-goal/SKILL.md) — sustain a terminal outcome with freely chosen execution and peer coordination.
- [Dynamic Workflow](../../coordination/meiva-dynamic-workflow/SKILL.md) — generate a Python script to coordinate peers and aggregate results.
- [Long-running work](../../coordination/meiva-being-a-long-running-agent/SKILL.md) — arrange event delivery, bounded waits, and recovery without repeated model turns.
