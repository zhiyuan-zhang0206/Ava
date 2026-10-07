---
type: doc
title: Notice Operation Receipts
description: Transactional notice creation and resolution identities and original-result recovery.
tags: [gateway, notices]
---

# Notice Operation Receipts

Notice creation and resolution accept optional `Idempotency-Key` headers.
`notice_operation_receipts` owns each normalized request and its immutable
result, scoped by concrete HTTP path and the credential-aware key. The receipt
and all notice/reply mutations commit together. Concurrent retries serialize
in Postgres; changed inputs conflict (409). Keyless legacy calls keep their
existing semantics, including distinct later read-with-reply operations.

A resolution receipt holds its original optional inbound id. Retry repairs
its wake tail, with the exact pending-row resurrection guard preventing revival
of completed work. An answer or dismissal which originally succeeded replays
instead of returning an already-resolved conflict. Bare reads also have a
receipt without manufacturing an inbound.

A creation receipt keeps the original local id, pending summary and superseded
ids. Replaying after another notice was posted does not supersede it or publish
an old posted event. Current task existence and expiration checks run only for
new operations; an accepted notice retains its receipt after those facts change.
All creations for an agent lock its row to serialize local id allocation.

Receipts have no TTL pruning: expiration cannot turn a known operation into a
new side effect. Any future retention design needs explicit expired-key
semantics. The SDK `notify(idempotency_key=...)` supports explicit recovery;
relative expiration inputs require their normalized absolute deadline to be
reused. The mixed-version rollout gate sends new keys while withholding
ambiguous automatic retries when gateway support is unproven.

Browser resolution allocates an operation id per invocation and optionally
accepts one from its caller. It does not persist or automatically retry requests.
CLI and IM bridge resolution remain keyless single attempts; this PR does not
add ambiguous-failure retries to those callers.
