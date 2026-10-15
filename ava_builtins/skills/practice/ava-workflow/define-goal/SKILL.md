---
name: define-goal
description: "Defines a verifiable outcome and stopping conditions. Use when the requested result or acceptance evidence is unclear."
---

# Define Goal

Turn intent into an outcome a normal peer can pursue and verify. Use this method
only for missing goal definition; a clear user requirement can go directly to
execution or [goal pursuit](../../../coordination/ava-goal/SKILL.md).

## Check the Goal

A usable goal states:
- What concrete result will be true and which artifact or system it concerns.
- What observable evidence supports completion: an objective check, a justified
  threshold, or an anchored qualitative judgment appropriate to the task.
- Which scope, permissions, budget, and other constraints must remain intact.
- Which condition calls for waiting, stopping, or a human decision instead of grinding.

Inspect available context first. Repair wording within settled intent; ask one
concise question with a recommendation when a missing validator or boundary
would change the outcome. Use honest observable criteria rather than decorative
numbers. For research, name the decision it must enable and the evidence standard;
for a bug, name the reproduction and failing-then-passing check.

For exploratory or creative work, define the decision, insight, or artifact the
work should enable and how it can be inspected. Qualitative criteria need
concrete examples, boundaries, or an identified reviewer; do not invent a score
to make them appear measurable. Learning can be a legitimate result, supported
by a tested prediction or a judgment on a fresh case rather than agreement alone.

[Align](../align/SKILL.md) owns purpose, preferences, and shared understanding;
Define Goal makes the desired result and stopping conditions explicit;
[Evaluate](../evaluate/SKILL.md) chooses and checks the evidence. If feasibility
or scope depends on an uncertain capability, use
[Capability–Timescale](../capability-timescale/SKILL.md) before making it a
commitment. When evidence changes the goal or its standard, make the revision
explicit instead of quietly lowering the bar to declare completion.

Example: "Fix the reproduced duplicate-charge bug in checkout; verify with the
existing regression check, preserve the payment API, and ask before any live
payment operation."

## Hand Off or Continue

Keep the result concise; use durable working notes or an existing work record when persistence
is needed. Choose execution, supervision, collaboration, and evaluation through
[Workflow](../SKILL.md) independently. A goal definition neither starts a
supervisor nor authorizes new spending. If supervision is selected, load its
skill and give the peer the objective, evidence, boundaries, and stopping rules.
