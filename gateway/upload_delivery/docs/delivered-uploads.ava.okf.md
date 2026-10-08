---
type: doc
title: Delivered upload acceptance and recovery
description: Immutable physical batches, versioned native copy and one retained chat outcome.
tags: [gateway, uploads]
---

# Delivered upload acceptance and recovery

`POST /api/keyed/v1/agents/{id}/uploads` requires a verified credential, explicit
`principal-v1` and caller `Idempotency-Key`. It always promises one user chat
**after** confirmed copy. Browser file delivery uses this route with one key per
submission; silent native-image attachments retain their separate upload path.
SDK/MCP callers are not activated. Old gateways have no such write route; do not downgrade this intent
onto synchronous legacy `POST /api/agents/{id}/uploads`. Agent IDs must be
positive signed BIGINT values, validated before native storage or database work.

The original ordered display names, bytes/SHA-256, sizes and content types identify
the request. Same key with different valid input returns 409. A fresh manifest
validates suffix/name length, URL delimiters, ASCII content type and object order
before claiming files. Display names remain metadata; finals use bounded UUID,
ordinal and true suffix, with a 217-byte basename budget that also fits the
native create-only temporary name. `Acceptance` is the immutable historical 202 snapshot,
not evidence that a URL remains accessible, remote files remain present or an
agent ran. Auth is repeated for every replay and read; no persisted bearer secret.

## Native ownership and receiving

`base.agents.upload_delivery.source` owns `upload_delivery_batches`. Tx A takes
the existing agent upload xact gate, consults retained key identity **before**
mutable target existence, then freezes source/target `UnitIdentity`, manifest,
physical root and verified `InboundProvenance`. The target is the unique serving
runner unit whose URL matches the machine's registered Ops address. Receiver
compares actual machine/home; a changed registry URL cannot silently redirect a
batch to another home. Unbound or ambiguous fresh placement rejects before claim.

Source receiving files are published outside a borrowed DB connection with
`create_private_bytes`, create-only verification and directory fsync. Tx B commits
original 202 acceptance and pending delivery intent together. Receiving identity
and reservation never expire; interrupted source publication needs original key
and bytes. Ready replay precedes mutable target/deletion checks. No cleanup of
finals, unbound staging or other attempts is performed.

Physical finals use `~/Downloads/AvaAgent-{id}/.delivered-v1/{batch}/{object}`.
`agent_upload_dir` is independent of `AVA_HOME`: two homes on one machine may
share this root. `storage.check_quota` serializes through the existing agent xact
gate and counts actual flat files, legal hidden finals and receiving reservations
bound to machine/resolved directory. Same batch paths count once across source
and receiver homes. Old silent receiving rows have no physical binding and are
conservatively retained in accounting, never assigned or deleted by inference.
Native primitive dot-temp files are staging; hard-crash leftovers are held and
excluded beside manifest reservation, without erasing them. Legacy flat unknown
files count normally. Legacy source and upgraded remote dispatch admission share this quota owner.
Remote HTTP finishes before borrowing its pool; the short file-write transaction
charges only net bytes/files when replacing an existing flat path.

New legacy writers reject the reserved directory basename. Even an old remote
writer sanitizes separators into flat names and cannot overwrite nested finals.
Legacy writers have no durable reservation: a disconnected late flat writer can
still exceed quota; this existing boundary is not claimed fixed.

The authenticated manifest-bound GET `.../uploads/{batch}/objects/{ordinal}`
serves only ready source-owned objects, after hash/size verification. It accepts
no arbitrary filesystem path, survives historical agent-row deletion, sends
attachment/nosniff headers and is separate from old native-image upload URLs.

## Gateway round and versioned receiver

Each Gateway lifespan retains its own recovery handle for start and close;
`app.state.upload_recovery` only exposes that handle to HTTP handlers. A later
app-state replacement cannot leave the original recovery TaskGroup unstopped.

`gateway.upload_delivery.worker.UploadRecovery` belongs to Gateway lifespan,
including pure Gateway topology without Ops. Four intents run in a scoped
TaskGroup; due selection and final acceptance borrow short transactions, with no
DB connection held across RPC/HTTP. Backoff is scheduling, never a lease or proof
another writer died. Multiple Gateway processes can repeat the same batch.
Source placement is checked before copy and again under locks before inbound.

The independently known Ops kind `upload-receive-v1` requires explicit integer
protocol 1 (missing/bool/float/unknown refused), exact target unit and strict
manifest. It receives no transport idempotency key and does not use uncertain
RPC response caching. Unknown old consumers reject before effects; no fallback
to overwriting `upload_receive`. Native source/copy metadata and quota admission
commit first. Download and create-only publication borrow no DB connection;
receiver commit follows actual verify/fsync. Network work has a bounded round.

Every receiver retry verifies/fills actual objects, including a historical ready
copy. Conflicting finals never get replaced. Missing ready objects require quota
reservation again before fill; wrong home, different manifest, source unavailable
or incompatible protocol cannot become a usable copy proof. Native readiness
requires the supported version, unit, manifest digest and absolute batch namespace.
A same-unit shortcut also verifies actual source objects; machine name alone is
insufficient. No promise survives operator deletion of irreplaceable bytes.

## One inbound and honest status

`source.complete` locks the retained intent and checks supplied immutable source,
target and manifest first. Existing accepted IID/outcome returns before mutable
target checks, even after IID/agent deletion; it never
recreates a deleted IID. Only pending state can accept a fresh late proof. HOLD
is not automatically unsealed by another overlapping worker.

For pending delivery, the transaction locks current agent/placement and matching
registered unit, then calls `insert_chat_inbound_in_transaction` with fixed
batch-derived ID, original user source and frozen verified provenance. Original
IID, copy proof and delivery outcome commit in the same transaction. Rollback
creates neither acceptance nor chat; lost commit response recovers the original
outcome. No mutable-target FK or TTL deletes this evidence.

Authenticated `.../uploads/{batch}` projects receiving/pending/accepted/HOLD,
frozen source/target, attempts, next retry, safe failure reason and retained IID.
Accepted means chat acceptance, not execution. Pending receiver outages back off;
unsupported protocol, native conflict/quota, missing source or changed/deleted
placement HOLD visibly. There is no operator cancel/unseal/retarget endpoint in
this contribution. Restoration or cancellation requires a separate reviewed policy.

Wake/resurrection follows the existing exact pending-inbound policy. A bounded
rotating scan revisits original still-pending rows; one failure does not starve
later batches and no mark-woken receipt can swallow a lost wake. Deleted/done IID
is never requeued. Gateway business pause/quiesce stops fresh work. Lifespan stops
rounds and drains actual shielded native futures before closing the pool; cancelling
an HTTP wait does not pretend its filesystem/DB thread ended. Ops retains its
existing maintenance admission and native-worker drainage.

See [silent upload owner](../../routers/docs/upload-batches.ava.okf.md) for existing
legacy and native-image compatibility. This is server Outbox recovery of one
upload domain, not a generic workflow or client Outbox.
