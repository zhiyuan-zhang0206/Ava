# Prefer native lifecycle and operational recovery over closure proofs

## Decision

The user authorized removing the four assurance mechanisms identified by the
2026-10-07 audit before designing replacement guarantees. Keep ordinary start,
serve, stop and backup paths working; do not rebuild custody under another name.

- Core service protocol readiness gates serving. Optional capabilities are
  reported unavailable and may recover independently; they do not impose a
  separate CLI startup wait or a global failure verdict.
- Application services use their launched child and ordinary bounded process
  signals/waits. Remove durable spawning/custody records, descendant census,
  closure-proof reconciliation and persistent replacement locks.
- Scheduled backups use existing isolated scratch directories, bounded worker
  cancellation and native temporary-PostgreSQL shutdown. Remove process-family
  receipts, cleanup-proof acceptance, blocked-kind custody and retirement flows.
- Read-only health checks observe protocol responses. Remove host listener/PID
  census and generation-bracketing proofs. Availability is not native ownership.

Preserve the macOS signed-helper permission ancestry, the root instance lock,
authentication, database/data-directory ownership, backup encryption, validated
artifacts and atomic publication. These own useful work or real authority; they
are not a promise of arbitrary-descendant disappearance. Actual signal owners
retain the small known-process identity checks needed to avoid signaling reused
PIDs. Concrete operation errors are still reported.

## Rationale and consequences

Repeatedly proving complete native process cleanup expands launch, stop,
readiness and recovery into another framework. It does not create a durable
cross-platform containment boundary. The user prefers smaller native mechanisms
and agent/user operational investigation of residual processes or OS stalls.

A successful ordinary operation does not certify that no detached process or
unobservable resource remains. Read-only protocol status does not authorize
signaling an arbitrary listener. An unexplained failure or a passing rerun is
not a reason to add another scan, receipt or blocking state automatically.

Future protections require a concrete observed problem and a separate discussion
of the smallest useful boundary. A repository merge is not a production rollout.

## Historical records

Earlier custody, family-retirement and identity-bound readiness decisions remain
historical evidence. This decision supersedes their requirements for the four
mechanisms above; it does not rewrite incident narratives or claim that their
original failures were repaired. Current component documentation owns the
resulting implementation.
