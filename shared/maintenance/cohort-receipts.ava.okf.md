---
type: doc
title: Receipts need a live continuation
description: A failed wake leaves a receipt only while the agent still has a continuation in the hold; another machine's woken agent, or a member whose drain is certified, never blocks a unit's hold.
status: current
---

# Receipts need a live continuation

Preparation captures the cohort under row locks: every non-terminated agent of
the machine, each as a restart command or parked. The host
(`services/agent_host/maintenance.py`) then records no failure or undelivered
receipt for a failed wake in two cases, only logging it at debug and setting
no fence:

- the agent is outside the captured cohort (`MaintenanceHold.outside_cohort`):
  another machine's agent, or one this hold never drains. A hold the cutover
  created has an empty cohort, so this covers every agent under it;
- the agent drained and the drain is certified
  (`MaintenanceHold.drained_and_certified`): the hold reached `drained`, which
  required every member drained or reaped with no unsettled failure, or any
  later phase.

Neither agent has a continuation left in the hold. The wake of a drained
member reads its row and returns: no restart command is pending, so the held
control that claims lifecycle commands never runs, ordinary inbound messages
stay pending, and the resume wakes it to process them. Both facts come from
the local journal, so the rule holds when the row read never ran. That is
the case it exists for (FC-10 F20): every runner receives every wake (the
dispatcher's subscription is cluster-wide), and a stop's shutdown cancels all
tasks before `_read_stored_config` can say "not ours". Receipts latched after
the stop started had no exit.

Everything else records as before: any failure before the capture (phase
`preparing`), a member's failure before the drain is certified (phase
`draining`, drained members included; `ava maintenance repair` covers both),
and a parked member's failure in any phase. The cutover adoption settles only
legacy receipts outside the cohort (`MaintenanceHold.receipts_outside_cohort`,
[cutover runbook](../../conventions/cutover-home-adoption.md#a-legacy-stop-hold-with-failure-receipts)).

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, grading of failure receipts and the repair exit.
