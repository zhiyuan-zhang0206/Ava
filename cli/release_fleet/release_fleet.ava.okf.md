---
type: doc
title: Fleet release workload policy
description: The fleet release transition - one coordinator decides, units follow; this node holds its pure workload policy (captured bounds, frozen cohort, start-barrier and watch-window verdicts, alert routing, known-good publication).
tags:
- cluster-lifecycle
- release
---

# Fleet release workload policy

`cli/release_fleet/` is the fleet release transition: the coordinator and
its phases ([[cli/release_fleet/coordinator.ava.okf.md]]) and the channel to
remote units ([[cli/release_fleet/channel.ava.okf.md]]). This node is its
workload policy: pure functions over typed records, with no clock, file,
database or network access. The coordinator journals each result, then
executes it. The legacy health probe, heartbeat liveness pass and their alert
rows are not evidence here.

## Policy block (`policy.py`)

`FleetPolicy` is the request's captured `policy`: `drain_s`, `close_s`,
`cancel_grace_s`, `start_s`, `watch_s`, `threshold_percent` (default 20),
`min_affected` (default 2), `alert_route` and `acknowledged_rejection`.
Bounds are whole seconds and the threshold an integer percent, so boundaries
are exact. `close_s` and `cancel_grace_s` are the writer-closure bounds
`cli/release_transition/local.py` applies at every unit's `stopping` (30 s and
10 s by default); its SIGKILL observation bound stays mechanism.

`exceeds(affected, cohort)` is `affected >= min_affected` and
`affected / cohort > threshold_percent / 100`: exactly at the threshold does
not recover, and one agent of a three-agent cohort never does.

## Cohort

Captured once at `quiescing` from every included unit's drained maintenance
hold (`drain_report`): its restart commands (agents whose runtime was live)
and its reaped receipts (turns the bounded drain cancelled). Idle `parked`
agents and units excluded before dispatch are not members. Every included
unit appears once, the gateway unit among them, even with no agents. An
unfinished drain or unsettled failure has no cohort outcome. A unit is keyed
by machine and the home it records itself (`machine_units.home`), in its own
platform's normalized absolute form, so a Windows runner's `C:\...` home is
a valid key on a Linux gateway.

## Verdicts (`workload.py`)

Evidence is samples over `[since, now]`. A sample older than `since` is
ignored, except a unit's recorded `failed` mark, which stays failed. A sample
newer than `now`, or naming a unit or agent outside the operation, is a
coordinator defect and raises.

An agent is **affected** by: its unit failed or unknown; a runtime-class
error or an outcome-unknown quarantine within the interval; and, when the
interval closes, no sample at or after the close (`unobserved`) or a
not-live one. The shared core is `gateway`, `database`, `pooler`, `redis`,
`schedules`, `delivery`, `fence` and `authorization`. Each needs a sample at
or after the close and no failed sample in the interval; a failed or unknown
gateway unit is a gateway failure whatever its samples say.

| Stage | Condition | Candidate | Previous |
|---|---|---|---|
| start (`since` = gateway start) | core failure or unknown | recover | hold |
| start | failed/unknown units' agents exceed the threshold | recover | hold |
| start | otherwise | proceed; failed units marked | proceed |
| watch, before `since + watch_s` | failed core sample, or definitive affected exceed the threshold | recover | hold |
| watch, before the end | otherwise | watch | watch |
| watch, at or after the end | core failure or unknown, or affected exceed the threshold | recover | hold |
| watch, at or after the end | anything affected, or a unit failed or unknown | commit degraded | commit degraded |
| watch, at or after the end | nothing | commit clean | commit clean |

The start barrier judges agents only through their unit; agent samples are
refused there because agents resume afterwards. Before the window ends only
facts that can only grow count (recorded unit failures, runtime errors,
quarantines, failed core samples), so an early recovery is the end decision
made early. Recovery happens at most once: the previous direction holds.
The signal an agent fact is read from does not grow (a completed turn clears
`last_turn_fatal_at`), so the coordinator journals each agent's first
sighting of each fact in the window (`first_sightings`,
`FleetProgress.window_facts`) and judges it with every later sample: an
agent affected mid-window keeps the release from known-good, errors spread
across samples add up to the threshold, and a continuation keeps them. An
error that appears and clears between two samples is not observed. A
sighting adds a reason, never an observation of liveness. The journal keeps
per-agent detail as ids (cohort, sightings, alerts) and one small affected
entry whose unit the cohort names. That still grows with the cohort, so
`capture_cohort` refuses one above `MAX_COHORT_AGENTS` (2,000): the release
aborts at `quiescing`, restarting the previous image unchanged, rather than
holding at `watching` when a failing verdict outgrows the 256 KiB journal.
The worst failure at the ceiling (every agent reaped, then sighted with a
runtime error and a quarantine, seven-digit ids) writes about 238 KB
(`test_cohort_ceiling.py`).

## Alerts (`alerting.py`)

Events: `unit_failed`, `unit_unknown` (per unit, with its agents),
`drain_cancelled`, `threshold_exceeded`, `recovering`, `held` (critical),
`recovered`, `degraded_commit`. Each alert is delivered as an `alerts` row
(source `release-fleet`, the shape `base.telemetry.alerts.upsert_alert` ingests),
plus the out-of-band webhook and an observer-agent notice when the route
names them. The webhook URL stays in `$AVA_HOME/secrets/<webhook_file>`, never
in the request; that file must be this user's and mode 0600, or delivery
refuses it. The coordinator journals each alert by `key` at first
emission and re-delivers from the journal, so a retry keeps `starts_at`.
Each hold is its own alert (a `held` key carries its time): an operation
held again after the operator continued it alerts again.
Without the database only the webhook can land.

## Known-good publication (`publication.py`)

`publish(prior, completion)` returns the next `releases/fleet-state.json`:
`aborted` writes nothing; `recovered` stays on previous and appends the
candidate to the append-only rejections; `degraded` moves to the candidate
without known-good; `clean` moves to the candidate and makes it known-good
only when the cohort was non-empty. Failed, unknown and excluded units stay
stale until converged. `require_admissible` refuses a rejected candidate
unless `acknowledged_rejection` names the latest operation that rejected it.

## Ruled choices

The plan left these open. Each conservative choice below was ruled accepted
as recorded on 2026-09-27
([decision](../../decisions/2026-09-27-unit-join-pitr-closure-fleet-policy.md)
item 3):

1. Integer `threshold_percent` replaces the plan's `threshold=0.20`.
2. Degraded also covers affected agents below the threshold, not only failed
   units, so such a release never becomes known-good.
3. A clean release with an empty cohort does not become known-good.
4. One failed core sample in the window recovers; there is no debounce.
5. At the window end every core signal, unit and agent needs a sample taken
   at or after the end.
6. All eight core signals are always required; a remote-managed or disabled
   component must be reported by the coordinator as a sample.
7. `watch_s` defaults to 1800 and `start_s` to 660 (the current root start
   bound); the plan gives neither.
8. The webhook is optional; a request may name none.
9. The alert route names a secrets file, not the webhook URL.
