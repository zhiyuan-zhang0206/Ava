---
name: ava-workflow
description: "Selects a working strategy for complex or unclear tasks. Use when calibration, planning, coordination, or sustained verification would improve execution."
---

# Ava Workflow — Choose How to Work

Workflow is the lightweight entry point for choosing a working strategy. The
human supplies intent and preferences; agents discover facts in the world and
choose how to pursue and verify the result. Loading this skill does not require
an interview, a plan document, goal supervision, or delegation. Direct execution
with a suitable check is a valid workflow.

## Choose a Working Strategy

First assess ambiguity, scale, consequences, duration, and independent work.
These are separate dimensions: a small consequential action can need alignment,
while a large repetitive migration can need script orchestration without an
interview. Respect the user's explicit choices and existing authorization.

| Dimension | Available choices | Selection question |
|---|---|---|
| Understanding | Existing context, targeted reconnaissance, Calibrate | Which factual uncertainty changes the work? |
| Intent | Direct execution, brief clarification, deeper Align | Which unresolved choice changes the outcome or authority? |
| Goal definition | Use the stated outcome, sharpen it briefly, Define Goal | Is completion clear and verifiable? |
| Continuation | Finish this turn, sustained goal supervision, event-driven waiting | What should start the next useful action, and what should stop it? |
| Collaboration | Work alone, reuse or spawn a few peers, Dynamic Workflow | Does delegation or a reusable script improve this task? |
| Evaluation | Direct observation, deterministic checks, independent peer review, adversarial review | What evidence is sufficient for these consequences? |

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
For a clear local task, reproduce the issue and verify the result. Start with a
small real slice before widening an investigation or generating orchestration.

Load [capability-timescale](capability-timescale/SKILL.md) when a feasibility,
schedule, or AI-dependent estimate rests on an unverified capability. Reuse
current evidence; do not perform capability research for every ordinary task.

## Delegation decisions

Delegate when the task has a clear scope, a useful independent execution context,
and results you can verify. Keep simple local steps with the current agent; weigh
supervision and integration cost against the benefit of another worker. Choose
tools by demonstrated capability for the task, not fixed file-count thresholds.
For Ava's external-worker launch, supervision, resume, or takeover mechanics,
load the external-agents guide in Ava Guide.

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

For substantial Align or Plan outputs, name selected skills and tools with a
short reason. Mention a rejected alternative only when its trade-off matters.
A routine task needs no capability report or mandatory `Not used` entry.

## Keep the Strategy Proportional and Adjustable

Use a sentence or a few working notes for ordinary tasks. Persist the objective,
acceptance evidence, boundaries, next action, and necessary coordination state
when work spans turns or needs recovery. Use durable working notes or existing work records; no collaboration plugin,
task registry, label convention, or specific SDK is required.

Calibration, alignment, planning, and evaluation can stand alone, interleave,
and feed back into one another. Execute settled slices while investigating
others when safe; delegation is optional. Reassess the remaining work at useful
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

For substantial implementation, recurring bottlenecks, or work that must survive
interruptions, read [working-principles](references/working-principles.md).
It covers investing in future work, causal investigation, early feedback, and
presenting decisions. A simple question or initial requirements discussion does
not need that full procedure.

## Detailed Guides

- [Calibrate](calibrate/SKILL.md) — investigate and correct factual understanding.
- [Align](align/SKILL.md) — resolve material intent, priority, and authority choices.
- [Define Goal](define-goal/SKILL.md) — sharpen an unclear outcome and its evidence.
- [Plan](plan/SKILL.md) — record dependencies and checkpoints when useful.
- [Work & Evaluate](work-eval/SKILL.md) — execute and verify against the chosen criteria.
- [Goal pursuit](../../coordination/ava-goal/SKILL.md) — sustain a terminal outcome with freely chosen execution and peer coordination.
- [Dynamic Workflow](../../coordination/ava-dynamic-workflow/SKILL.md) — generate a Python script to coordinate peers and aggregate results.
- [Long-running work](../../coordination/ava-being-a-long-running-agent/SKILL.md) — arrange event delivery, bounded waits, and recovery without repeated model turns.
