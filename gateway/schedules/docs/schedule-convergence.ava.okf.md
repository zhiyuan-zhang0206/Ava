---
type: doc
title: Schedule mutation receipts and revision convergence
description: Transactional operation identities and recovery of accepted schedule revisions.
tags: []
---

# Schedule mutation receipts and revision convergence

`PUT /api/schedules/{id}` and `POST .../start`, `.../stop`, `.../restart`
accept an optional `Idempotency-Key` (1–128 characters). The existing
`Idempotency-Scope: principal-v1` binds a key to the verified credential and
route; unsupported scopes or a scope without a key return 400. Unkeyed legacy
requests remain separate intents and cannot be automatically retried safely.
The CLI and browser mint one legacy key per invocation, without silently activating principal-v1 on an unnegotiated server; retry code must reuse that
key and immutable request. No client outbox is involved.

## Acceptance transaction

The schedule owner reserves/locks a row in `schedule_operation_receipts`, compares
the immutable target/body, locks the schedule, changes desired state, queues
`schedule_sync_requests`, and saves the original response in one transaction.
Concurrent same-key requests return that response; changed payloads return 409.
Replay is checked before current enabled state or existence, so an old restart
receipt cannot re-enable a schedule subsequently stopped or deleted. Receipts
have no automatic expiry: no prune can turn a delayed retry into a new restart.
The receipt reports durable acceptance, not script completion.

Same-value edits add no version/revision/sync. A real config/enabled change,
built-in config resync, or explicit restart increments `desired_revision`.
Start on a completed/error schedule also creates a new deliberate revision;
start on an enabled nonterminal schedule and stop on a disabled row are no-ops.
A new revision resets the crash budget once, in its mutation transaction.
Repeated consumer attempts honor persisted launch backoff and breaker limits.

## Convergence recovery

The runner command carries schedule ID and revision. The PTY owner's allocation
metadata proves the live execution revision; its allocation generation remains a
separate release identity checked through the existing exact-session cleanup.
A PostgreSQL advisory transaction gives one manager ownership of a schedule's process effects, including across manager processes; conditional revision
writes prevent it acknowledging a later desired revision. A matching live revision is adopted and marked `applied_revision`; a predecessor
is officially reaped before replacement. Missing provenance for a versioned
live session leaves the request queued. Failed reap or launch also stays queued,
with inspectable status/error and crash budget. One failed schedule does not
block consumption of unrelated requests.

Runner admission conditionally marks the revision applied and reads its script
only if the row remains enabled at that desired revision. A delayed obsolete
launch executes no script. Admission occurs before user code, so a quickly
completed process remains applied even if its manager died before recording the
launch. Retrying an unacknowledged applied completed/error revision does not
rerun it. Normal supervised crash recovery remains available and can execute
process code again; this contract does not promise exactly-once external effects.

Reconcile also discovers pending revisions. It always reads current desired
state rather than replaying an old request's enabled/config payload. Maintenance
holds defer execution; stops/deletes retain exact PTY cleanup and orphan run
closure. Deletion enqueues cleanup in the deletion transaction. The existing
`schedules/catchup.py` at-most-once cron slot claims and accepted callback-loss
window are unchanged.

The initial-command metadata survives schedule-manager restarts while its PTY
owner remains alive. PTY daemon restart is a process-loss boundary: it does not
re-adopt terminals, and its durable exact-birth ledger sweeps old identities.
A live versioned session with unavailable metadata remains uncertain/queued;
normal supervisor recovery after verified process loss is a separate crash
recovery policy, not receipt replay or proof of exactly-once script execution.
The optional list metadata is opt-in, preserving the old client's response
shape. An older PTY service cannot supply this evidence and must be upgraded
before a pending versioned live session can be safely adopted.
