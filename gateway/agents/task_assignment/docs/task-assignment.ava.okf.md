---
type: doc
title: Guarded compound task assignment
description: Atomic original task and agent acceptance with independent native launch recovery
---

# Guarded compound task assignment

`POST /api/keyed/v1/task-assignments` requires `Idempotency-Key` (1–128 characters),
`Idempotency-Scope: principal-v1` and a verified request principal. The fixed
versioned path makes an older router fail without creating either object.
The SDK never falls back to separate spawn/create calls for that intent.
The route is AtLeastOnceWithKey but retains conservative ambiguous-failure
transport retries (`legacy_keyed_retry=False`). Retrying an entire execute_code
script is outside this contract.

## Intent and SDK admission

`ava.tasks.create_and_assign(..., operation_key="intent")` requires a valid
explicit key. The SDK always uses atomic acceptance; no `require_idempotency`
flag or keyless multi-transaction recipe remains. Return type is `(Task, agent_id)`.
Both objects describe the original business acceptance, not runner readiness or
current task state. Use task/agent reads for current state.

The request contains a positive non-bool `actor_agent_id`, task title,
description, positive parent id, canonical priority and requested reminder
interval, plus agent label, machine and config. Unknown fields and enum values
are rejected. The SDK preserves `None` interval intent and puts an explicitly
supplied preset in config; omitting a preset uses current gateway defaults.
It defaults machine to its own machine before HTTP. Actor/machine/config
changes under the same verified principal and key conflict; identical raw Python
arguments from another context do not necessarily produce the same request.

The SDK's identity owner requires a lease-free established agent id. Strong
admission refuses any borrowed ExternalLease before HTTP or application database
writes. ExternalLease callbacks cannot be serialized or revalidated by the remote
transaction after lock waits.
Server actor/spawner fields are execution provenance, not proof of lease authority
or a new security ACL. Scope comes from verified HTTP authentication, method,
actual versioned path and raw key. Cookie and bearer credentials for the same
verified administrator share a principal; revoked credentials cannot replay.
Different verified principals have independent namespaces, without a promise of
sharing the same pair across them. No browser or MCP compound consumer is added.

## Transaction and concurrency

Freeze the semantic request before preset/default settlement. A short first
transaction locks and looks up the compound receipt. Missing receipts use the
existing spawn preflight outside that transaction, avoiding a nested pool borrow
with pool size one. The business transaction locks and looks up again: a winner
that committed during preflight is authoritative. On preflight failure, one more
short lookup returns a committed winner if present; otherwise the original
preflight error propagates.

Fresh acceptance locks/validates the parent and uses native cursor-owned birth
and task creation writers. Birth metadata, task, audit facts, assignment inbound
and immutable receipt commit together. Exceptions after any writer roll them all
back. Parent/title policy and notification fencing remain in their existing
owners. There are no child-step keys, extra outbox, or generic workflow engine.

`task_assignment_receipts` owns the immutable request, original complete Task
snapshot, positive agent id and original launch attempt, plus a historical
first-accepted birth snapshot (machine, config_overlay, birth_config, preset name,
attempt). It has no FK or automatic TTL. Receipt deletion would permit repeating
an operation; retention must preserve tombstones. Missing/unknown Task fields or
invalid stored vocabulary fail instead of filling defaults or recreating rows.

## Acceptance and launch

After commit the route returns 201 with the original `task`, `agent_id` and
`launch_attempt_id`. Independent `launch` observation uses the existing
SpawnedAgent model. `launch_failure` and `retry_launch_path` describe a failed
native dispatch; a failure does not undo acceptance, rotate the attempt or create
another agent. Unexpected observation failures are logged and returned as a
bounded failure description while retaining the accepted pair. SDK callers
receive the accepted pair even when launch is pending; the pair is never a claim
that the new agent is ready or executed the task.

Same-key replay reads the retained pair before mutable preflight, title, parent,
or reminder policy checks. Renaming/closing/deleting the task or deleting birth
metadata does not re-create the pair or assignment notification. Live hints are
best effort; the existing inbound queue/watchdog recovers a missed delivery hint.

Launch recovery uses current native birth metadata and its existing admission
policy. Only the original still-current attempt is considered; an operator's
rotated attempt is not overwritten. Completed/terminated or missing births are
not resurrected by replay. The historical placement/config snapshot remains
unchanged for inspection, but is not written back over current operator changes
and does not lock the running configuration to historical values. This endpoint
does not call the attempt-rotating retry-launch API. Launch/execution are outside
the atomic database acceptance boundary.
