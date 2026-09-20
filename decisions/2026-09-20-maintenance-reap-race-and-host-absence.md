# Reap receipt order and independent host-absence evidence

## Context

Review of the maintenance recovery fix found two gaps. The database reap mark
commits before its journal receipt. The interrupted turn can record its failure
in between, so rejecting a reap receipt for a member with a failure strands a
mark the drain already applied. A missing health listener also does not imply
a missing host process: startup or listener failure can leave active work
without a serving health endpoint.

## Decision

Accept a reap receipt for a cohort member even when its failure arrived first.
The failure remains audit evidence and the derived unsettled-failure predicate
excludes it after reap certification. Keep the predicate consistent in status
and raw-journal triage. Ordinary failures without a reap remain blockers.

Use the existing service, pidfile and home-scoped process checks before
skipping the identity probe in stop or repair. A running host must answer;
connection refusal and unreadable process evidence retain the hold.

## Alternatives rejected

- Reject failure-before-reap while dropping failure-after-reap: arrival order
  would decide whether an already-applied reap can release maintenance.
- Infer host absence from a refused HTTP dial: that proves listener absence,
  not process or continuation absence.
- Add lease renewal to fix the ownership exception: renewal already runs in an
  independent daemon task and covers busy rows. A fresh-lease database test
  shows that the reap's `restarting` status alone causes the ownership check
  to fail. Actual renewal starvation would require separate runtime evidence.

## Verification and boundary

Regression tests fail against PR #3015's original head and exercise both
receipt orders through the real database drain, live-host listener refusal
through stop and repair, and raw-journal triage. This change does not alter
lease renewal or compaction admission. Whether repeatedly interrupted agents
need compaction admitted before ordinary turns remains a separate design
question; context size alone does not establish lease expiry.
