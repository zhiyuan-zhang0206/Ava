---
type: doc
title: Pending normal release planning
description: Pre-stop normal plan validation with activation deliberately disabled.
---

# Pending normal release planning

The normal release module builds and validates a sealed per-unit plan; it does
not activate that plan. The checked chain (`_drive_checked_normal_release`) is
wired immediately behind the activation gate: journal-staged migration receipt,
selector CAS, bootstrap stop by exact retained identity, pinned-order gated
service starts with exact spawn receipts, and the unit readback. The gate
refuses first, so no production path reaches the chain until checked recovery
and exact spawn receipts replace it. A bootstrap request may reference a private
normal request before its first stop, and standalone preparation
(`_update_normal_release_standalone.py`) can consume an existing candidate-ready
bootstrap handoff from a deep-crash state: a bootstrap that is already stopped
is deferred to the stop stage's exact-identity adjudication (a live one is
still challenged), the selector may equal the predecessor or the prepared
pointer, and the retained handoff's dead owner may be the predecessor or a
recovery-lineage claim (live or foreign owners refuse). Both paths bind the
exact exited predecessor, operation, challenge, verified image, complete
preparation receipt and pending all-unit plan. Unsupported readiness transports
refuse during preparation while bootstrap still serves. Source update flags and
git/uv/converge fallbacks are unavailable in this mode.

Every execution entry point fails before updater ownership, selector writes,
bootstrap stop or service spawn. The standalone entry's ownership re-take
(host mutex, `resume_bootstrap` only after exact owner death, CAS-guarded
`clear`) is retained behind that fence, so the flip needs no further entry
work. Activation remains disabled until each durable phase has checked forward
or reverse recovery and service start returns an exact PID/birth spawn receipt
that closes the post-fork/pre-SessionRecord ambiguity. The preparation models
and identity adapters are retained as inputs to that future recovery-aware
activation path.

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

This planner does not yet implement normal/source first handoff, complete
non-session/job quiesce, or all-unit collection/migration orchestration. The
preparation receipt's unknown closure is not promoted to a positive permit. Unix-only/native services without
a verified readiness adapter remain pre-stop refusals. These are implementation
gaps, not claims awaiting CI.

Tests cover exact selector serialization, separate receipt/inventory digests,
unsupported or mutable commands, CLI source isolation, pinned service order,
retained unfinished recovery, strict journal transitions, the pre-effect
activation fence, and the late-stage standalone preparation (stopped
bootstrap, advanced selector, dead lineage owners) with its claim order — the
claim test runs the real handoff writer. The chain's stage order, refusal
propagation, gate-before-seat posture, and the continuation/standalone routing
are pinned. Actual normal full-roster cold launch and the complete
distributed transition remain required evidence.
