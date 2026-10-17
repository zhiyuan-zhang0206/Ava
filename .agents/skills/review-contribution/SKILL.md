---
name: review-contribution
description: "Reviews Meiva contributions against project rules. Use before opening a PR or checking a change's design, consumers, and validation."
---

# Review your contribution

Review your own diff against the user's goal and the current source of truth.
Then request an independent person or agent to review the actual contribution.
This is a contributor workflow requirement, not an automated semantic CI gate.
Use [docs/contributing.md](../../../docs/contributing.md) for the contribution path.

1. Read [AGENTS.md](../../../AGENTS.md) and the affected component's local `docs/`.
   Trace changed values, state transitions and boundary contracts through their
   real consumers with `scripts/audit/where_used.py`. Check behavior against the
   domain owner, including unknown, missing and failure-path inputs.
   For statuses, outcomes, policies or modes, follow
   [finite-domain vocabulary](../../../docs/conventions/python-conventions.md#finite-domain-vocabulary):
   inspect canonical members, raw-input conversion, consumer coverage and stored
   or wire compatibility. Check the relevant domain decisions for its rationale.
2. Consult [Python conventions](../../../docs/conventions/python-conventions.md)
   and [import layering](../../../docs/conventions/import-layering.md) for the
   changed code's layer, ownership and structure. Do not invent a parallel set of
   architectural rules in the review.
3. For dependencies or a new abstraction, consult
   [technology selection](../../../docs/conventions/technology-selection.md) and
   [philosophy](../../../docs/conventions/philosophy.md). Check whether an existing
   third-party or project capability already owns the responsibility.
4. Match validation to the changed behavior and its consumers using
   [testing](../../../docs/conventions/testing.md). Check that evidence exercised
   the defect or contract, and distinguish skipped, missing and successful checks.
   Regenerate derived artifacts from their owners.
5. Reconcile current documentation using
   [documentation maintenance](../../../docs/conventions/doc-maintenance.md).
   Re-read the final diff for unrelated changes and record material verification
   gaps in the PR description. Discuss unresolved design choices with the user.

6. Have an independent reviewer check the actual diff and relevant source,
   documentation and test assertions. For comment migration, follow
   [structured code comments](../../../docs/conventions/engineering/structured-code-comments.md):
   distinguish redundant prose from useful rationale, verify retained statements,
   and check changed behavior against unchanged contracts. Static reference
   checks do not establish semantic truth or test execution.

Summarize findings and resolutions in the ongoing task or PR description. No
model service in CI, fixed report format, approval-SHA comment or signed report
is required. This check does not authorize merging or runtime operations.
