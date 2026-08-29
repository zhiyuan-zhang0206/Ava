# PITR retention begins with a local fail-closed dry-run plan

## Context

Physical backup now has immutable WAL/base objects and generation-pinned restore
proofs, but remote deletion remains deliberately absent. Retention arithmetic
crosses candidate, proof, WAL-continuity and timeline-ancestry evidence; combining
that selection logic with deletion would make the first rollout destructive.

## Decision

Introduce only a local dry-run planner. It retains the latest two protected
chains by strictly parsed UTC candidate capture identity. Any unprotected
candidate blocks eligibility and remains fully pinned. Every WAL decision joins
the local uploader ACK to the viewer-only remote inventory on archive name,
object name, generation, size, CRC32C and immutable metadata; neither source can
replace the other. The planner keeps the continuous WAL recovery window from the
oldest retained base through the exact joined ACK high-water. Cross-timeline
objects are pinned and block eligibility until authenticated history ancestry is
implemented. Unknown, malformed, missing,
forked, generation-ambiguous or concurrently changing evidence blocks the plan
and forces the eligible set empty.

The canonical plan is written atomically as a private 0600 local file with file
and directory fsync. Identical evidence produces identical bytes and digest.
The existing scheduler may refresh it behind a new default-off flag; health and
`ava pitr retention inspect` expose the result. No module in this slice has a
remote-delete method, credential or production enable.

## Consequences

- Operators can observe eligibility and byte cost before any destructive API exists.
- Candidate/proof/ACK/inventory corruption blocks cleanup instead of producing a partial plan.
- Daily and pre-update logical `pg_dump` retention is unchanged.

## Alternatives considered

- Planning and deletion together: rejected because the first policy rollout
  would also be the first destructive rollout.
- Object age: rejected because age does not prove a restorable chain.
- Manifest-only WAL: rejected because PITR needs continuous acknowledged WAL.
