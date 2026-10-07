---
type: doc
title: Guarded page registry acceptance
description: Atomic replacement and observed-row close with historical principal-bound receipts.
tags: []
---

# Guarded page registry acceptance

`POST /api/keyed/v1/agents/{agent_id}/pages` accepts the existing
`PageRegisterRequest`. `POST /api/keyed/v1/agents/{agent_id}/pages/{name}/close`
requires `expected_page_id`, the strictly positive numeric registry ID the
caller observed. Both require a valid 1–128 character `Idempotency-Key`, exact
`Idempotency-Scope: principal-v1`, and a principal supplied by credential
verification. Authentication runs again on every request; a receipt does not
bypass revoked credentials. Keys are scoped by that principal, HTTP method and
actual versioned path. Deliberate new operations use new keys; a retry keeps
its original path, key and validated body. Never downgrade an uncertain guarded
request to a legacy path.

`page_operation_receipts` stores a canonical validated request fingerprint and
the original `PageRow` acceptance. It has no agent/page foreign key or expiry:
registry cleanup, target termination/deletion and subsequent registrations must
not turn an old accepted intent into a fresh effect. Changed request content
under the same scoped key returns 409. Receipt lookup precedes mutable target,
host validation and TTL/default evaluation. A same-key replay returns the original
201 registration or 200 close representation and publishes no old page events.

One writable transaction acquires the scoped-key gate, then the existing
agent-page transaction gate. Fresh acceptance checks the target and, for
registration, its current status and permitted dial target. Replacement closes
the previous page, inserts the new row and stores its acceptance in that same
transaction. Any failure before commit rolls back all three. Concurrent duplicate
requests share one registry ID and original TTL; different keys represent
separate serialized replacements. Live-port uniqueness is still authoritative;
the SQL helper uses a savepoint to explain a raced port conflict without
committing or discarding the caller's outer transaction.

Guarded close verifies that the latest registry row for the original name has
the observed numeric ID. A mismatch or missing row returns 409 before effect,
so an observation of A cannot close a later B after name reuse. A matching already
closed row accepts a new close intent without changing its snapshot. Original
close receipt replay bypasses current target checks and returns the historical
acceptance even after B appears or the target is removed. No fresh nonexistent
agent is accepted.

The public legacy paths and SDK signatures remain one-shot. Their HTTP page
mutations join the same agent-page gate; registration now commits close and
replacement together rather than exposing a separately committed close on
failure. The existing committing SQL wrappers remain available; explicit
`*_in_transaction` primitives leave commit ownership with their caller. Older
gateways do not know the versioned paths and cannot execute them. This server
slice does not activate SDK/UI guarded calls or automatic ambiguous retries;
existing route retry gates remain conservative during mixed-version operation.

A `PageRow` proves registry acceptance, not server readiness or content delivery.
The returned historical URL is still the existing name-based reverse-proxy URL:
it may now resolve to a newer same-name page, or fail after cleanup/expiry. Its
numeric row ID remains the acceptance identity. Current registry GET is the
state authority. PageOpened/PageClosed events remain best-effort hints only on
fresh acceptance; a receipt replay does not retransmit obsolete name-based close
hints. Existing page-server supervision, health tokens, runtime readiness waits
and server launch/kill ownership remain separate; no client outbox is added.
