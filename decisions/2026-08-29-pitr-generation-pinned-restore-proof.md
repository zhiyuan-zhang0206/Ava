# PITR protection requires a generation-pinned physical restore

## Decision

A physical base candidate becomes protected only after Ava downloads every
input by exact GCS object name and generation, verifies size, CRC32C and the
complete immutable metadata map, and restores it in an owned sibling directory.
The restore reader has a viewer-only credential distinct from the uploader
credential and exposes no write, list-latest, or delete operation.

The multi-GiB base ciphertext is downloaded once into a 0700 quarantine after
a same-filesystem capacity preflight. Ava authenticates the local ciphertext in
one complete AES-GCM pass, then reopens the same owned object for a second local
authenticated pass through the safe tar extractor. This avoids a second
cross-region download while preserving the rule that unauthenticated bytes are
never usable PostgreSQL data. WAL and timeline-history objects are likewise
bound to the protected manifest's exact generation and encryption header.

The drill starts PostgreSQL only from the sibling scratch tree, on a private
socket and isolated port. Its restore command accepts only the durable local
allowlist derived from the pinned objects. Unknown filenames, missing timeline
history, generation changes, WAL gaps, authentication failures, and live
PostgreSQL identity changes fail closed. A protected manifest is an immutable
new object; the candidate manifest is never edited in place.

## Crash and trust boundaries

Every restore run has durable ownership evidence. Startup reconciles stale
work before creating another run; an identity it cannot prove is preserved and
reported critical. Cancellation owns the complete scratch PostgreSQL process
tree and proves it absent before removing scratch data. The final proof payload
is durably staged before remote publication, so a crash between the remote ACK
and local publication reuses identical bytes instead of permanently colliding
with the immutable object.

The proof records exact generations, replay target and achieved LSN, stable
database fingerprints, verification durations, and bytes downloaded. It is a
recovery claim, not merely evidence that PostgreSQL accepted a connection.

## Rollout boundary

`AVA_PITR_RESTORE_PROOF_ENABLED` is independently default-off and requires the
WAL and base-candidate gates. This change does not enable `archive_mode`, delete
remote objects, implement N=2 retention, deploy configuration, or replace any
logical dump. The first rollout therefore remains protected by the existing
migration-bearing `pg_dump`; physical protection exists only after a real
generation-pinned drill publishes its proof. Retention remains a later boundary.
