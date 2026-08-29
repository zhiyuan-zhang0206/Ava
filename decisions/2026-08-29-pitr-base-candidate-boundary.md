# PITR base candidates stream from one immutable plain tree

## Decision

Weekly physical bases are a separately gated, disabled-by-default gateway
service. A run creates one plain `pg_basebackup -Fp -X none` candidate, verifies
it locally, and atomically publishes the directory before any remote I/O. The
shared backup lock covers birth and local verification only; deterministic
preflight and GCS upload happen after the lock is released, so daily and
pre-update logical dumps retain priority.

The remote representation is canonical tar → zstd → AES-GCM. Directory order,
tar headers, compression parameters, nonce, object name, and key id are pinned.
The first traversal computes the exact ciphertext size and CRC32C and durably
publishes a 0600 plan. Upload reopens the candidate and reproduces the same
bytes through an 8 MiB bounded stream. It never writes a complete tar or
ciphertext locally. The bounded WAL uploader remains file-only and unchanged.

This release can publish only `protected=false` candidate manifests. A
candidate is not a recovery promise: WAL ACK continuity, generation-pinned
download, a second `pg_verifybackup`, isolated replay, smoke verification,
`protected=true`, and chain-aware deletion belong to the next boundary.

## Why

Production has about 19 GiB free while PGDATA is about 6.27 GiB. A compressed
tar plus a same-sized encrypted staging object creates avoidable disk pressure,
and sharing the WAL worker would let a multi-GiB upload delay the RPO-critical
segment stream. One immutable plain candidate plus bounded buffers keeps the
disk and scheduling boundaries explicit.

`AVA_PITR_ENABLED` and `AVA_PITR_BASE_BACKUP_ENABLED` remain separate. This
allows WAL archiving to be observed before the weekly service is enabled. The
first rollout is not protected by this mechanism; existing daily and
migration-bearing pre-update `pg_dump` artifacts remain unchanged.

## Security and restore invariant

The independent 0600 backup key and GCS generation preconditions remain
mandatory. Exact generation, size, CRC32C, and the complete immutable metadata
map must match on create and on 412 reuse. AES-GCM authenticates only when its
final tag is verified, so a future restore must decrypt into quarantine and
must not unpack bytes into a PostgreSQL data directory before authentication
finishes.

The first implementation rejects custom tablespaces. Runtime enablement also
requires a local, least-privilege PostgreSQL replication identity accepted by
`pg_hba.conf`; credentials never enter argv or logs.
