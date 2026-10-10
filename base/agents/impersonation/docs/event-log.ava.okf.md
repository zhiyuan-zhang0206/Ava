---
type: doc
title: Impersonation event log
description: Producer-written SDK/audit event rows, source receipts, and the in-database completeness predicate.
tags:
- base
- impersonation
---

# Impersonation event log

Decision: `docs/decisions/agents/impersonation/2026-10-02-impersonation-event-log-in-postgres.md`.

The record of what a borrowed identity did lives in `agent_impersonation_entries`,
the lease's permanent, immutable, sequenced log. New automatic leases carry
`event_delivery_protocol_version = 2`; manual leases and older leases carry none,
keep no event log, and report `pending_reason` `manual` or `legacy`.

## Writers

Both halves append through `event_log.append_source_event`, so nothing is read
back from the telemetry store. A controller's SDK event is not an audit event and
the entry holds its body. A central audit event's entry holds a reference to its
`audit_events` row (`event_uid`, the stream id `id`, `line_sha256`) in place of a
second copy of the body; `history.entries` resolves the reference, so the
hand-off export and every reader see the full event, and an unresolvable
reference raises. The append records the `audit_events` row first (idempotent),
so the reference never dangles. A duplicate delivery of the same event key is
compared on resolved content, whichever side holds the body.

- **Central audit events** are appended by `record_central_event` in the producing
  transaction, under the lease row lock that also closes admission. A rolled-back
  operation leaves no row and a committed one needs no emit to survive. Their
  `source_key` is `central`. The same transaction records the tagged event in `audit_events` (`record_audit`), the global record every audit fact has, and the entry points at that row. The audit-root census test classifies every producer.
- **A controller's SDK events** are appended synchronously by `capture_local_event`
  by the call's explicit admission callback before the emit queue, while the
  controller's receipt is open. The `source_key` is the receipt's key. This is effect-then-write: a hard
  crash between a call's effect and its row leaves the source unsealed, so the
  lease stays pending.

The emitted event still goes to Loki as an observation copy, tagged with
`impersonation_session`. SDK sampling precedes capture, so a sampled-out call is in
no record and the SDK sampling policy stays `unknown` in the hand-off.

## Receipts and the completeness predicate

A controller opens a receipt (`agent_impersonation_event_participants`) when it
attaches and seals it with its row count when it detaches. Close first closes local
admission and waits up to `AVA_IMPERSONATION_EVENT_SEAL_WAIT_SECONDS` for admitted
calls; a call still running seals its own source when it drains. The wait never
marks a source empty or failed.

The Attachment owns its original `LocalCaptureGate`; each public SDK recorder
snapshots that owner from the process-local `ava.context` entry and retains a
separate admission until its final event. No participant lookup, process registry
or task-local current admission participates in capture. Detach and a later
attachment cannot redirect an old call's event or failure to the new receipt.
Direct local audit producers pass the attachment's explicit capture callback to
`emit()` / `emit_prepared()`; `ava.skills` also passes it through its reported audit
write. Plugins producing eligible local audit events must pass that owner explicitly.
Events without a capture dependency have no local receipt writer.

Receipt capture executes outside the best-effort observation sink boundary.
Unknown writer or seal failures propagate; the body is never repeated. When a
body or capture error is already primary, cleanup and failure-recording errors are
exception notes on that exact original error. Known database availability loss
retains the existing failed-receipt recovery. A pending failure stays on its original
gate until persistence returns; a known failed-receipt refusal or filesystem seal
failure remains pending and reported instead of being called complete.

The database enforces the log's closure: a trigger accepts a source row only while
its receipt is open (or, for `central`, while admission is open), and the seal
procedure refuses a count that differs from the rows.

`finalize_impersonation_event_log` marks a lease complete (`events_completed_at`, a
lifecycle entry, a rebuilt hand-off export) when it has ended, admission is closed,
and every receipt is sealed with a matching count. It runs from the seal
procedure, from a trigger on `ended_at`, and from the admission-close procedure, so
every end path (release, expiry, abort, agent termination) completes in its own
transaction when no source is still open. Release itself refuses while a receipt is
open or failed. No external store, certifier or loop takes part.

A failed receipt never completes its lease. A lease that ended with an open
receipt is pending as `awaiting_participant_seal`.

## Signal

State signal only, no thresholds. The gateway ttl reaper emits
`impersonation_event_log_incomplete` on every pass, per lease and condition, while the
row fact holds: `seal_stuck` for an ended lease still waiting on an open receipt (clears
when it seals), `capture_failed` for a lease with a failed receipt (permanent, because the
lease can no longer complete). The alert rule over the event stream owns the notification.

## Hand-off statistics

`event_delivery.state` is `complete` or `pending` with a `pending_reason`; coverage
is `unknown` while pending, so a zero count is never evidence of zero calls.
`completion_basis` is `source_log`.
