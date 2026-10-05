---
name: define-goal
description: "Clarifies an unclear outcome, acceptance evidence, scope, and stopping conditions. Use when goal definition needs work; skip when the user's existing requirement is already verifiable. Defining an outcome does not activate sustained supervision."
---

# Define Goal

Turn intent into an outcome a normal peer can pursue and verify. Use this method
only for missing goal definition; a clear user requirement can go directly to
execution or [goal supervision](../../../coordination/ava-goal/SKILL.md).

## Check the Goal

A usable goal states:
- What concrete result will be true and which artifact or system it concerns.
- What evidence proves completion and the applicable binary or numeric threshold.
- Which scope, permissions, budget, and other constraints must remain intact.
- Which condition calls for waiting, stopping, or a human decision instead of grinding.

Inspect available context first. Repair wording within settled intent; ask one
concise question with a recommendation when a missing validator or boundary
would change the outcome. Use honest observable criteria rather than decorative
numbers. For research, name the decision it must enable and the evidence standard;
for a bug, name the reproduction and failing-then-passing check.

Example: "Fix the reproduced duplicate-charge bug in checkout; verify with the
existing regression check, preserve the payment API, and ask before any live
payment operation."

## Hand Off or Continue

Keep the result concise; use the existing task or working notes when persistence
is needed. Choose execution, supervision, collaboration, and evaluation through
[Workflow](../SKILL.md) independently. A goal definition neither starts a
supervisor nor authorizes new spending. If supervision is selected, load its
skill and give the peer the objective, evidence, boundaries, and stopping rules.
