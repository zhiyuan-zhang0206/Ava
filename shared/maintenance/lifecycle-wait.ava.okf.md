---
type: doc
title: Bounded lifecycle wait during preparation
description: Preparation retries, under the same row locks, when it meets in-flight work it did not author — then aborts if it outlives the bound.
status: current
---

# Bounded lifecycle wait during preparation

Preparation freezes its cohort in `maintenance_cohort.prepare`. When unfinished
work belonging to another actor stands against a cohort member or a parked
agent — a `restart`/`terminate` command, or claimed ordinary work such as a
claimed chat — the pre-#3591 behavior refused immediately. Now preparation
raises a typed `LifecycleCollisionError` **before** the capture is persisted,
and the caller (`ops.agent_pause._prepare`) bounded-waits: it retries under the
same `(holder, acquired_at)` CAS and row locks until the work resolves, up to
`settings.gateway.pause_lifecycle_wait_seconds` (default 300s, the same
drain/Phase-A envelope; 0 refuses immediately), then aborts with the waited
result in the message. An early resolution exits the wait, so the bound is only
the give-up point, and a successful retry re-derives the cohort from the
resolved world, so a resolution cannot double-admit.

Waitable work is anything without a maintenance payload: an ordinary agent
lifecycle command against a cohort member, and claimed ordinary work on a
parked agent. Maintenance-authored commands (`payload.maintenance`) keep their
immediate refusal — a migration-class operation never enters the wait path, and
a mix refuses too. One `pause_lifecycle_wait` event records each episode
(`waited_s`, `outcome`: `resolved` / `exceeded` / `refused`); a collision-free
preparation emits nothing.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, and the admission gates.
