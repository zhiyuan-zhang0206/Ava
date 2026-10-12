---
type: doc
title: Bounded lifecycle wait during preparation
description: Preparation consumes orphaned ordinary claims on parked agents and bounded-waits unfinished lifecycle work under the same row locks.
status: current
---

# Bounded lifecycle wait during preparation

Preparation freezes its cohort in `cohort.prepare`. Before the
collision guards run, it consumes orphaned ordinary claims on the parked agents
selected by `_classify`, inside the same transaction holding their
`agents_meta` row locks. Only `status='claimed'` rows with
`payload->'maintenance' IS NULL` and kind other than `restart`/`terminate`
qualify. Agents in the host-absent `cold` set stay excluded: their ordinary
claims do not block `_unresolved_parked` and remain untouched for boot.

Every qualifying claim becomes `done`, whatever its age: the runtime that took
it is gone, and like a cold boot (`reconcile_claimed_inbounds`) preparation
never returns such input to `pending` for redelivery. The sender must resend if
the work still matters. Every update compares `id` and `status='claimed'`; a
missed CAS leaves the newer state intact.

One `pause_orphan_claim_settled` event per changed row records `agent`,
`message_id` and `age_s` after transaction commit. A later collision rolls the
consumption back and emits no events.

Unfinished lifecycle work belonging to another actor raises a typed
`LifecycleCollisionError` **before** the capture is persisted, and the caller
(`ops.agent_pause.prepare`) bounded-waits: it retries under the
same `(holder, acquired_at)` CAS and row locks until the work resolves, up to
`settings.gateway.pause_lifecycle_wait_seconds` (default 300s, the same
drain/Phase-A envelope; 0 refuses immediately), then aborts with the waited
result in the message. An early resolution exits the wait, so the bound is only
the give-up point, and a successful retry re-derives the cohort from the
resolved world, so a resolution cannot double-admit.

Ordinary `restart`/`terminate` commands retain their existing wait/refuse
behavior. Claimed ordinary work on a live cohort member stays with its runtime
and normal drain. Maintenance-authored commands (`payload.maintenance`) keep
their immediate refusal, and a mix refuses too. One `pause_lifecycle_wait`
event records each episode
(`waited_s`, `outcome`: `resolved` / `exceeded` / `refused`); a collision-free
preparation emits no wait event.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, and the admission gates.
