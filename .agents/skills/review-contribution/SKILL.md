---
name: review-contribution
description: Reviews your Ava contribution against current project and domain rules before opening a PR. Use when preparing a contribution or checking its design, consumer coverage and validation gaps.
---

# Review your contribution

Review your own diff against the user's goal and the current source of truth.
This is a contributor self-check, not a separate reviewer role or merge gate.
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

Summarize useful findings in the ongoing task or PR description. No reviewer
agent, fixed report format, approval-SHA comment, external notification or
review-publication step is required. This check does not authorize merging or
runtime operations.
