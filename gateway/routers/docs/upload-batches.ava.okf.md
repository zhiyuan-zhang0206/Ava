---
type: doc
title: Silent upload acceptance and immutable objects
description: Keyed silent batches, durable reservations and same-body recovery.
tags: []
---

# Silent upload acceptance and immutable objects

`POST /api/agents/{id}/uploads?deliver=false` accepts optional `Idempotency-Key`
through `optional_request_key`, including verified `principal-v1` when explicitly
requested. The stored key includes the HTTP method and actual agent path. The
ordered manifest fingerprints original filenames, bytes, sizes, content types and
delivery policy. Same-key changes return 409; different keys create new objects.
Keyed `deliver=true` is unsupported (422 before admission); the whole route remains
`NON_IDEMPOTENT`. Unsupported gateways are never automatically retried.

`gateway.routers.upload_batches` owns `agent_upload_batches`. A first writable
transaction takes a per-agent PostgreSQL xact lock, checks existing identity before
mutable agent existence, and commits the receiving manifest/reservation. A second
writable transaction takes the same lock, publishes files and commits the original
receipt. Receiving identities/reservations never expire or become fresh intents.
Ready receipts replay after target deletion: acceptance does not prove that the
historical URL is currently accessible. Fresh missing targets return 404.

Files use flat `ava-upload-<uuid>-<ordinal><suffix>` names; original names remain
display metadata. Suffixes preserve image MIME consumers and must fit 128 UTF-8
bytes without URL delimiters `?`, `#`, `%` or control characters; NUL in filenames
is refused. An absent suffix is valid. Names stay
below filesystem limits without truncating long display names. Legacy writes
reject the reserved `ava-upload-` prefix, preventing overwrite even by a writer
that continues after losing its DB connection; other legacy names retain behavior.

Each attempt owns separate hidden staging. Existing `create_private_bytes` fsyncs
complete staging files; create-only hard links publish final objects. Existing
objects must match size/hash. Final directories are fsynced before the ready
receipt commits. Recovery validates present objects and fills missing ones. A
disconnected old writer can only publish identical immutable bytes; no attempt
replaces or deletes finals or cleans another attempt's staging.

Quota admission counts receiving manifests once, excluding their already-published
finals from the disk baseline. Legacy admission shares the xact gate. Legacy
writers have no durable reservation: DB disconnection followed by late file writes
remains an uncovered quota window. Orphan staging and abandoned receiving
reservations have no automatic cleanup; recovery needs the same key and bytes.
Receiving identities must not be pruned to release quota.

Browser silent uploads mint one key per `uploadFiles` invocation or accept an
explicit reusable key. Cross-call recovery requires retaining that key and files;
there is no browser outbox, persistence or automatic ambiguous retry. Upload
progress measures transferred bytes, not committed acceptance. Remote pull,
`deliver=true` notifications and their crash recovery remain outside this owner.
