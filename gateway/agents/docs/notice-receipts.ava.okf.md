---
type: doc
title: Notice Operation Receipts
description: Transactional notice creation, resolution and guarded observed-row mutation receipts.
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

## Guarded current-notice mutations

`PATCH /api/agents/{id}/notices/current/guarded-v1` and
`POST /api/agents/{id}/notices/current/dismiss/guarded-v1` require a valid caller
key, explicit `Idempotency-Scope: principal-v1`, and verified authentication on
**every** call. `observed_notice_id` is the positive global `NoticeItem.id` from
the feed/inspector, not the zero-based local id returned by SDK `notify()`.
A fresh operation rejects an absent or resolved observed row with 409; it never
edits or withdraws the later notice. Missing agents return 404 on fresh calls.

The existing receipt table stores the explicit fields (omission differs from
null) and original 200 `NoticeItem` snapshot. Concrete path, method and principal
isolate these keys from creation, resolution and each other. Receipt lookup
precedes mutable target/expiration checks, so replay survives superseding or
physical deletion. The result records historical acceptance, not current
activity, delivery or URL availability. Receipts have no FK or TTL pruning.

Fresh mutations take the agent `FOR NO KEY UPDATE` lock before the notice row
lock, serialize with creation's `FOR UPDATE`, and commit mutation plus receipt
in one write transaction. Reply inbound FK `KEY SHARE` locks remain compatible.
Existing `resolved_at IS NULL` eligibility remains unchanged; this introduces
no new expiration policy. New edit input rejects null priority/blocking,
unknown fields, blank titles and empty edits; content null clears the content.
Only fresh commits publish row-specific live hints; replay does not announce an
old notice. Those hints remain best effort and are not a durable delivery proof.

Legacy current selectors retain their 204 behavior and conservative single
attempt contracts. SDK/UI callers are not activated here. Unsupported servers
reject the guarded path before effects; a strong intent must never fall back
to a legacy selector after an ambiguous response.
