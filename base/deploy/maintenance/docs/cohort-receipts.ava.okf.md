---
type: doc
title: Receipts need a live continuation
description: A failed wake leaves a receipt only while the agent still has a continuation in the hold; another machine's woken agent, or a drained or parked member once the drain is certified, never blocks a unit's hold.
status: current
---

# Receipts need a live continuation

Preparation captures the cohort under row locks: every non-terminated agent of
the machine, each as a restart command or parked. The host
(`services/agent_runner/agent_host/maintenance.py`) then records no failure or undelivered
receipt for a failed wake in two cases, only logging it at debug and setting
no fence:

- the agent is outside the captured cohort (`MaintenanceHold.outside_cohort`):
  another machine's agent, or one this hold never drains;
- the drain is certified and the agent drained or is parked
  (`MaintenanceHold.settled_after_drain`): the hold reached `drained`, which
  required every member drained with no failure, or any later phase.

None of these agents has a continuation left in the hold. The wake of a
drained or parked member reads its row and returns: no restart command is
pending, so the held control that claims lifecycle commands never runs, and
ordinary inbound messages stay pending. The resume wakes drained members; the
host's pending-inbound scan picks up parked ones once the hold is released.
These facts come from the local journal, so the rule holds when the row read
never ran. That is the case it exists for (FC-10 F20): every runner receives
every wake (the dispatcher's subscription is cluster-wide), and a stop's
shutdown cancels all tasks before `_read_stored_config` can say "not ours".
Receipts latched after the stop started had no exit.

Everything else records as before: any failure before the capture (phase
`preparing`) and a member's failure before the drain is certified (phase
`draining`, drained and parked members included); `ava start` settles both.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, grading of failure receipts and the `ava start` settlement.
