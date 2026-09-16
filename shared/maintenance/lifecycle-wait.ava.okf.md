---
type: doc
title: Bounded lifecycle wait during preparation
description: Preparation retries, under the same row locks, when it meets an unfinished agent lifecycle command it did not author — then aborts if the command outlives the bound.
status: current
---

# Bounded lifecycle wait during preparation

Preparation freezes its cohort in `maintenance_cohort.prepare`. When an
unfinished `restart`/`terminate` command belonging to another actor stands
against a cohort member (or a parked agent), the pre-#3591 behavior refused
immediately. Now preparation raises a typed `LifecycleCollisionError`
**before** the capture is persisted, and the caller (`ops.agent_pause._prepare`)
bounded-waits: it retries under the same `(holder, acquired_at)` CAS and row
locks until the command resolves, up to
`settings.gateway.pause_lifecycle_wait_seconds` (default 90s; 0 refuses
immediately), then aborts with the waited result in the message. A successful
retry re-derives the cohort from the resolved world, so a resolution cannot
double-admit.

Only ordinary agent lifecycle commands are waitable. Maintenance-authored
commands (`payload.maintenance`) and claimed ordinary work keep their
immediate refusal — a migration-class operation never enters the wait path.
One `pause_lifecycle_wait` event records each episode (`waited_s`, `outcome`:
`resolved` / `exceeded` / `refused`); a collision-free preparation emits
nothing.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, and the admission gates.
