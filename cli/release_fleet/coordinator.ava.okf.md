---
type: doc
title: Fleet release coordinator
description: The gateway home's finite executor drives one fleet journal through its phases, aborts before the fence, recovers once after it, holds otherwise, and publishes the release state.
tags:
- cluster-lifecycle
- release
---

# Fleet release coordinator

A release is one fleet operation per cluster, owned by the coordinator: the
gateway home's finite executor (`execute.py` runs `coordinator.py` after
adopting its administrator authority). A single box is a fleet of one: no
remote unit, no listener, the same phases. Remote units are
[[cli/release_fleet/channel.ava.okf.md]].

## Request and journals

`request.py`: a `FleetRequest` (kind `fleet`) is the gateway home's request;
its previous/candidate/executor images are the gateway unit's, and it lists
every other registered unit as an included `UnitSpec` or an `Exclusion`
(`paused`, `offline`, `operator`), the captured `FleetPolicy` and, exactly
when units take part, the coordinator endpoint. Previous and candidate are
distinct commits over one schema. A `UnitRequest` (kind `unit`) is one unit's
home request; its id and `created_at` are the fleet's, so the maintenance
hold identity is the same everywhere. `inventory.py` requires every
registered unit to be the gateway, included or excluded, a paused machine's
always excluded; `require_fleet_of_one(home)` is the single-unit gate (PITR's
too); any listed unit refuses naming slices dbgen-8 and FC-9.

Both kinds are the home journal's `Operation`: `progress.py` adds
`FleetProgress` (admitted generation, units with their last instruction and
answer, frozen cohort, start and resume stamps, verdicts, alerts with
landed deliveries, the one decision, outcome) or `UnitProgress`. Histories
only grow and captured facts are set once (`require_successor`); an outcome
is recorded exactly at `complete`.

## Phases

prepared (read-only gates: topology, configuration, release history
admits the candidate, one admitted generation, unit preflight), dispatching
(the cluster deploy lease, unit dispatch), quiescing (drain, freeze the
cohort), stopping (units close, then the gateway: `close_s`,
`cancel_grace_s` from the policy), fencing, selecting, authorizing, starting
+ observing (the gateway root), starting_units (the start barrier,
`judge_start`), resuming, watching (`judge_watch` every 30 s until
`watch_s` after resume), complete. Every decision is journaled before it
acts: the cohort before its alerts, a verdict with its alerts and unit marks
before its recovery or commit; a continuation after executor death executes
a journaled verdict instead of judging again.

- **Abort** (before the fence): `restoring` restarts the unchanged previous
  image on generation n, recorded read-only at `prepared`; outcome
  `aborted`; nothing is published.
- **Recover** (once, a failing candidate after the fence): the predecessor on
  a new generation. From `watching` it drains again under a new maintenance
  hold; outcome `recovered`, the candidate rejected.
- **Hold**: any other failure (fencing, selecting, authorizing, resuming,
  restoring, the previous direction) journals the error and a `held` alert
  and exits; the operator continues with the same `ava cluster update
  --prepared`, which re-runs the held step. A hold verdict whose hold was
  carried out (its error recorded) is judged again rather than replayed;
  one journaled just before executor death is still carried out first. A
  step that holds again raises a new `held` alert.

A failure is any `Exception` a phase raises, a database error or a bug
alike: the route follows the phase, never the class
(`cli/release_transition/failure.py`). The journal, decision and alert keep
its class and message, the log its traceback. Only process-ending
`BaseException`s (`KeyboardInterrupt`, `SystemExit`) pass undecided; a
continuation resumes from the journaled phase, as after process death.

The lease (`deployment_state`, holder `fleet:<id>`) is taken at
`dispatching`, re-armed by a continuation before any effect, renewed by a
thread and released at completion; a lost lease fails the next step. A
renewal that raises is a missed round, retried until the lease could lapse
before the next one; one answered "not yours" loses it at once. An abort
decided at `prepared` or `dispatching` needs no lease.

## Gateway evidence, publication and alerts

`gateway.py` samples the shared core itself: the root's full readiness in
its own image (gateway, schedules, delivery), an administrator `SELECT 1`, a
Redis `PING`, the issued generation, and `verify_active` (pooler, fence);
cohort agents from `agents_meta` (live lease on their unit's machine, no
fatal turn since the interval began). Outcome-unknown quarantine has no
representation yet, so no agent is reported quarantined. Completion writes
`releases/fleet-state.json` once per operation (`publication.publish`).
Alerts are journaled by key at first emission; each delivery (alert row,
webhook, observer notice; `delivery.py`) is journaled once it lands and
retried at the next boundary otherwise. Completion does not wait on a
delivery that keeps failing: an alert still undelivered then is logged as an
error and stays in the journal, where `ava cluster release status` lists the
routes it has not reached.

## Recorded choices

Ruled on 2026-09-27
([decision](../../decisions/2026-09-27-fleet-core-release-choices.md)):
failures at `resuming` hold rather than recover (admission may be open); a
failed unit start is marked failed, with no automatic retry yet; a release
moves between two distinct commits (a same-commit rebuild is `adopt`'s); the
request builder runs in the admitted image until FC-7b.

Ruled on 2026-09-28
([decision](../../decisions/2026-09-28-fleet-coordinator-ruled-details.md)):
`stopping` is the plan's `closing`; `starting` + `observing` its
`starting_gateway` — the code's names win. Units' barrier deadlines are
journaled in their instruction, and a continuation keeps the original
deadline rather than recomputing one, adding only a 30 s re-answer window. An
abort at `prepared` completes as `aborted` rather than refusing.
