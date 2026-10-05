---
name: ava-workflow
description: "Chooses and adjusts how to work: direct execution, calibration, alignment, goal definition, sustained supervision, peer collaboration, script orchestration, and evaluation. Use automatically for non-trivial, ambiguous, consequential, sustained, or parallel tasks; choose only the capabilities the task needs."
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

## Capability and timescale calibration

Before setting scope, scheduling work, or estimating AI-dependent feasibility,
load [capability-timescale](capability-timescale/SKILL.md). Verify relevant
capability against current sources and, when practical, a representative probe.
Carry the evidence and uncertainty into Align's criteria and Plan's checkpoints.

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
   facts before assuming a capability is usable.
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

## Core Principles

### Invest in future work

Apply the system prompt's **Invest in the future** rule when choosing the work,
not only when closing a task. A request is often one step in maintaining a
system, running a business, or developing a research program. Infer that larger
purpose from the conversation and evidence; distinguish it from an unconfirmed
assumption. Optimize for the immediate result **and** the cost of the next
similar task. Do not make the human spell out every useful implication.

Before a substantial implementation, and whenever progress repeatedly stalls:

1. **Find the actual bottleneck.** Separate implementation, environment setup,
   build/test turnaround, external waiting, integration and rework. Use available
   logs and artifacts; do not blame CI or merge queues without evidence, or
   invent a time breakdown when none was recorded.
2. **Follow the recurring cause.** Trace it through tools, architecture, product
   assumptions and even the project's purpose; do not preset a layer at which
   diagnosis or change must stop. Reuse capabilities that fit and replace those
   that sustain the problem. Ground the intervention in causal evidence and
   name its current consumers. A substantial share of effort, such as 30%, can
   be worthwhile; that example is neither a quota nor a ceiling.
3. **Close the feedback loop early.** Exercise the smallest real path through
   the relevant system before expanding the implementation or test matrix.
   Automate reproducible setup, observation and cleanup when those are the
   repeated work. When a failure exposes a recurring gap, improve that shared
   path rather than require another disposable script or isolated patch.
4. **Test the diagnosis at concrete checkpoints.** Exercise real consumers and
   check for reproducible runs, fewer manual steps, faster useful feedback or
   removal of the recurring failure. Compare with the previous workflow where
   evidence exists. Use the result to continue, revise or abandon the approach.
   Checkpoints do not impose scope, time or percentage caps; do not force a
   return to the original feature while its recurring cause remains unresolved.

When replacing architecture, migrate all callers and delete superseded
entrypoints, compatibility shims and bootstrap tails as part of the same
completed integration. Verify real consumers through the replacement and check
that no caller still depends on the obsolete paths.
Package acquisition and platform permission brokers remain valid capabilities
when needed. Integrate them into the replacement under their authority checks;
removing obsolete wiring does not make those responsibilities forbidden.

Keep experimentation independent of promotion. For software, an isolated
preview can consume an unmerged remote or local branch without waiting for
green CI. Resolve the branch to a fixed commit or record an exact source
snapshot for each run; preserve the result's provenance and failures. Preview
answers a specific runtime question; it does not erase a failing CI check or
replace merge and production gates. Respect current machine, network, budget
and authority constraints when choosing the shortest useful feedback path.

Keep this reasoning short in the alignment/plan or working notes: **larger
purpose; causal evidence; intervention; current consumers; next checkpoint**.
Preserve reusable tools in the project and durable handoff state using
the `ava-being-a-long-running-agent` skill.
Explicit user constraints and resource limits still govern. New spending,
external effects and changes beyond existing authorization need the appropriate
authority; technical depth alone does not require another ceremonial approval.

### 1. Reality first, question second (Calibrate → Align)

Before asking the user anything, look for the answer in the environment — codebase, docs, config files, running state. Facts are discovered; material decisions need the user. When the user's model of the subject is uncalibrated, run the Calibrate loop first so the plan is grounded in reality. Then actively question — inspired by Matt Pocock's ["grill me"](https://github.com/mattpocock/skills) — working unresolved material decisions as a design tree in rounds, every question carrying your recommended answer. Existing instructions and confirmed decisions remain authorization; do not require another sign-off just because you restated them in a document. Settle genuinely open scope, trade-off and authority questions before dependent work.

### 2. Plan when execution demands it (Plan)

Record a roadmap when dependencies, duration, or coordination make it useful.
A few independent peer briefs or a dynamic workflow script can express the plan;
parallel work alone does not require a separate document or confirmation phase.

### 3. Check the work (Work & Eval)

Challenge the assumptions and exercise the relevant behavior as you work. Use
concrete acceptance criteria and record verification limits. Independent review
can help with unfamiliar or consequential changes; choose it when the user
requests it or the task benefits from another perspective. It is optional,
and parallel work or writing a plan does not require a reviewer agent.

The [engineering review](../ava-serious-engineering/practices/review/SKILL.md)
guide offers failure-path questions when useful. Project contributors follow the
project's contributing guidance; maintainers choose its merge process.

## Detailed Guides

- [Calibrate](calibrate/SKILL.md) — investigate and correct factual understanding.
- [Align](align/SKILL.md) — resolve material intent, priority, and authority choices.
- [Define Goal](define-goal/SKILL.md) — sharpen an unclear outcome and its evidence.
- [Plan](plan/SKILL.md) — record dependencies and checkpoints when useful.
- [Work & Evaluate](work-eval/SKILL.md) — execute and verify against the chosen criteria.
- [Goal supervision](../../coordination/ava-goal/SKILL.md) — sustain a peer's work toward a terminal outcome.
- [Dynamic Workflow](../../coordination/ava-dynamic-workflow/SKILL.md) — generate a Python script to coordinate peers and aggregate results.
- [Watcher](../../coordination/ava-watcher/SKILL.md) — wait for an event or time without repeated model turns.
