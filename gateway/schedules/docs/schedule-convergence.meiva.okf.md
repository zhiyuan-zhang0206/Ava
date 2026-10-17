---
type: doc
title: Schedule mutation receipts and revision convergence
description: Transactional operation identities and recovery of accepted schedule revisions.
tags: []
---

# Schedule mutation receipts and revision convergence

`PUT /api/schedules/{id}` and `POST .../start`, `.../stop`, `.../restart`
accept an optional `Idempotency-Key` (1–128 characters). The required
`Idempotency-Scope: principal-v1` binds a key to the verified credential and
route; a key without the scope, an unsupported scope, or a scope without a key
returns 400. Unkeyed requests remain separate intents and cannot be
automatically retried safely. The CLI and browser mint one key per invocation and
send it with the scope; retry code must reuse that key and immutable request. No
client outbox is involved.

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
A new revision resets launch admission once, in its mutation transaction.
Repeated consumer attempts cannot launch an already-consumed revision again.

## Convergence recovery

The runner command carries schedule ID and revision. The PTY owner's allocation
metadata proves the live execution revision; its allocation generation remains a
separate release identity checked through the existing exact-session cleanup.
A PostgreSQL advisory transaction gives one manager ownership of a schedule's process effects, including across manager processes; conditional revision
writes prevent it acknowledging a later desired revision. A matching live revision is adopted and marked `applied_revision`; a predecessor
is officially reaped before replacement. Missing provenance for a versioned
live session leaves the request queued, as does a failed reap, but only for a
bounded window: after ten minutes of one failure streak the manager records the
schedule as an error, emits `schedule_convergence_abandoned`, consumes the request
and leaves the process alone until a new desired revision (start, stop, restart,
edit, delete) authorizes another attempt; the give-up is in memory, so a
restarted service starts one new bounded streak. A maintenance hold is not a
failure and does not count. Before creating a process, the manager commits
`launch_count=1`, `applied_revision=desired_revision`, and an inspectable unknown-outcome
error. A failed or uncertain launch consumes that revision; it is acknowledged
without automatic replay. One failed schedule does not
block consumption of unrelated requests.

Runner admission conditionally marks the revision applied and reads its script
only if the row remains enabled at that desired revision. A delayed obsolete
launch executes no script. Admission occurs before user code, so a quickly
completed process remains applied even if its manager died before recording the
launch. Retrying an unacknowledged applied completed/error revision does not
rerun it. A missing started process is marked error and is not automatically recreated,
including after manager or PTY service restart. Losing a launch before script
execution is an accepted cost of this admission rule. Explicit start/restart
creates a new revision and can intentionally execute the script again.

Reconcile also discovers pending revisions. It always reads current desired
state rather than replaying an old request's enabled/config payload. Maintenance
holds defer execution; stops/deletes retain exact PTY cleanup and close the
in-progress run rows of the process they reaped (as does a launch), with no
periodic orphan-run sweep. Deletion enqueues cleanup in the deletion transaction. The existing
`schedules/catchup.py` at-most-once cron slot claims and accepted callback-loss
window are unchanged.

The initial-command metadata survives schedule-manager restarts while its PTY
owner remains alive. PTY daemon restart is a process-loss boundary: it does not
re-adopt terminals, and its durable exact-birth ledger sweeps old identities.
A live versioned session with unavailable metadata remains uncertain/queued;
verified process loss does not authorize script replay. An operator must
explicitly request a new revision to execute again.
The optional list metadata is opt-in, preserving the old client's response
shape. An older PTY service cannot supply this evidence and must be upgraded
before a pending versioned live session can be safely adopted.

Schedule resource creation has separate fingerprint-only receipts; see
[[gateway/routers/docs/resource-creation.meiva.okf.md]].
