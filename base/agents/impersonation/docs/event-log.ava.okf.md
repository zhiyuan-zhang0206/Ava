---
type: doc
title: Impersonation event log
description: Producer-written SDK/audit event rows, source receipts, and the in-database completeness predicate.
tags:
- base
- impersonation
---

# Impersonation event log

Decision: `decisions/2026-10-02-impersonation-event-log-in-postgres.md`.

The record of what a borrowed identity did lives in `agent_impersonation_entries`,
the lease's permanent, immutable, sequenced log. New automatic leases carry
`event_delivery_protocol_version = 2`; manual leases and older leases carry none,
keep no event log, and report `pending_reason` `manual` or `legacy`.

## Writers

Both halves append the event body itself (`event_log.append_source_event`), so
nothing is read back from the telemetry store.

- **Central audit events** are appended by `record_central_event` in the producing
  transaction, under the lease row lock that also closes admission. A rolled-back
  operation leaves no row and a committed one needs no emit to survive. Their
  `source_key` is `central`. The same transaction then records the tagged event in `audit_events` (`record_audit`), the global record every audit fact has. The audit-root census test classifies every producer.
- **A controller's SDK events** are appended synchronously by `capture_local_event`
  at the telemetry seam, before the emit queue, while the controller's receipt is
  open. The `source_key` is the receipt's key. This is effect-then-write: a hard
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

## Alerts

State alerts only, no thresholds. `ImpersonationEventSealStuck` fires for an ended
lease still waiting on an open receipt and resolves when it seals (the gateway ttl
reaper reconciles it each pass); `ImpersonationEventCaptureFailed` fires when a
capture fails and stays, because the lease can no longer complete.

## Hand-off statistics

`event_delivery.state` is `complete` or `pending` with a `pending_reason`; coverage
is `unknown` while pending, so a zero count is never evidence of zero calls.
`completion_basis` is `source_log`.
