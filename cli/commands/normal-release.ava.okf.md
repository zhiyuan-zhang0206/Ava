---
type: doc
title: Pending normal release planning
description: Pre-stop normal plan validation with the checked activation chain.
---

# Pending normal release planning

The normal release module builds and validates a sealed per-unit plan; the
checked activation entry (`execute_normal_release`) drives the checked chain
(`_drive_checked_normal_release`): journal-staged migration receipt, selector
CAS, bootstrap stop by exact retained identity, pinned-order gated service
starts with exact spawn receipts, and the unit readback. The flip (task #4117
S5) removed the activation fence and declared `CHECKED_ACTIVATION_READY` at
module level; the managed-writer mode gate consumes that declaration
fail-closed. A bootstrap request may reference a private
normal request before its first stop, and standalone preparation
(`_update_normal_release_standalone.py`) can consume an existing candidate-ready
bootstrap handoff from a deep-crash state: a bootstrap that is already stopped
is deferred to the stop stage's exact-identity adjudication (a live one is
still challenged), the selector may equal the predecessor or the prepared
pointer, and the retained handoff's dead owner may be the predecessor or a
recovery-lineage claim (live or foreign owners refuse). These standalone
entries are the coordinator's per-unit dispatch targets (task #4129 I6):
`run_normal_release` is the nominal drive entry (`--normal-release`), and
`run_normal_commit` (`--normal-commit`) records the committed stage after the
all-unit publication and disposes the retained envelope; its preparation skips
the stop-stage faces because the publication already consumed the pending
journal, and the restricted hop never self-drives. Both paths bind the
exact exited predecessor, operation, challenge, verified image, complete
preparation receipt and pending all-unit plan. Unsupported readiness transports
refuse during preparation while bootstrap still serves. Source update flags and
git/uv/converge fallbacks are unavailable in this mode.

Every execution entry point runs the same reconciliation under fresh
authority: each stage adjudicates its retained evidence first and performs only
the missing effect. The standalone entry takes host mutual exclusion, re-claims
the exact generation (`resume_bootstrap` only after exact owner death), and
CAS-guards its `clear`. Each durable phase has checked forward or reverse
recovery, and service start returns an exact PID/birth spawn receipt that
closes the post-fork/pre-SessionRecord ambiguity.

The versioned bootstrap envelope records whether a normal continuation was
planned before any normal write. Planned-but-absent, malformed, or unfinished
normal evidence prevents generic clear and both manual and automatic unpause.
Once present, the nested journal cannot be discarded by a bootstrap rewrite;
normal writes retain their request/operation/unit/selector identity and follow a
monotonic phase graph. Only a fully validated `committed` journal permits both
retained files to clear. These journal contracts do not themselves authorize
activation or recovery.

The selector and receipt path bind `prepared_receipt_digest`, the SHA-256 of the
complete sealed preparation receipt. Its internal `inventory_digest` remains the
narrower `ExpectedUnitWriters` tuple digest and must equal the planned unit
digest. The producer pins dependency order during preparation. Identity adapters
model native sessions, listener child PID/birth, executable/command, and service
health responses. Python normal services report their loaded module and image;
development responses remain unchanged. Native frontend/collector probes also
require native listener ownership, not just an HTTP success.

Every preparation read has the original challenge budget; no retry renews it.
Per-unit readbacks are recorded by the effect seat into the same pending
evidence for the all-unit coordinator. Current publication, not a local successful launch, remains the
terminal condition. This planning permission does not authorize ordinary agent
admission or service effects.

## Incomplete callers and support

This planner does not yet implement normal/source first handoff or complete
non-session/job quiesce. The all-unit collection and continuation orchestration
is connected (tasks #4129 I5/I6): the closing section collects the fleet's
closure, drives each unit's normal release, publishes the complete readback set
and runs each commit tail. The
preparation receipt's unknown closure is not promoted to a positive permit. Unix-only/native services without
a verified readiness adapter remain pre-stop refusals. These are implementation
gaps, not claims awaiting CI.

Tests cover exact selector serialization, separate receipt/inventory digests,
unsupported or mutable commands, CLI source isolation, pinned service order,
retained unfinished recovery, strict journal transitions, the flip's
module-level readiness declaration, and the late-stage standalone preparation
(stopped bootstrap, advanced selector, dead lineage owners) with its claim
order — the claim test runs the real handoff writer. The chain's stage order,
refusal propagation, activation-entry order, and the drive/commit-tail/
standalone routing are pinned. Actual normal full-roster cold launch and the complete
distributed transition remain required evidence.
