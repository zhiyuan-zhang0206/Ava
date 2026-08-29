# PITR owns an immutable object-store boundary

Date: 2026-08-29

## Decision

The physical-backup plane owns a narrow `ObjectStore` protocol. Its sole
production adapter uses the official `google-cloud-storage` client with
generation-zero conditional creates, CRC32C, bounded conditional retry, and
service-account credentials loaded from a private file. Object names are
content-identity-bearing and immutable. The uploader has no remote delete
operation; future chain retention owns deletion separately.

An upload becomes acknowledged only after generation, size, CRC32C, and
application metadata are verified and a private local ACK manifest is atomically
written and fsynced. A precondition failure is idempotent only when the existing
object has exactly the expected metadata and bytes; otherwise it is a critical
collision.

## Why

Calling `gcloud` would make a daemon depend on mutable host login and binary
state, while placing credential paths and command details at a subprocess
boundary. Direct REST would reimplement Google authentication, resumable upload,
checksum, precondition, and retry behavior. The official SDK already owns these
mechanics; Ava owns only the durability and collision policy specific to PITR.

This first adapter method is intentionally WAL-only. It stages at most one
seekable ciphertext for the single uploader worker and rejects source files
larger than 64 MiB before writing that stage. Weekly base backups are much
larger and must not use this file-staging boundary: their rollout owns a
separate restartable streaming upload contract so plaintext and ciphertext are
never both materialized in full.

Separating remote deletion prevents a retrying writer from accidentally turning
an upload or crash-recovery path into retention. It also makes the ordering
explicit: verified remote object, durable ACK, staging removal, spool removal.
