---
type: doc
title: Receipts belong to the cohort
description: Only an agent the hold's cohort captured can leave a failure receipt; a failed wake of any other agent records nothing, so another machine's woken agent never blocks a unit's hold.
status: current
---

# Receipts belong to the cohort

Preparation captures the cohort under row locks: every non-terminated agent of
the machine, each as a restart command or parked. From then on the host records
a failure or undelivered receipt only for a member
(`services/agent_host/maintenance.py`, `MaintenanceHold.outside_cohort`). Any
other agent has no continuation in the hold, so its failed wake is logged at
debug and dropped, with no fence. Before the capture (phase `preparing`, empty
cohort) nothing proves an agent foreign and every failure still records;
`ava maintenance repair` covers that phase.

The cohort is read from the local journal, so the rule holds when the row read
never ran. That is the case it exists for (FC-10 F20): every runner receives
every wake (the dispatcher's subscription is cluster-wide), and a stop's
shutdown cancels all tasks, catching wakes for other machines' agents before
`_read_stored_config` could say "not ours". The old host latched those as
blocking receipts on its own hold, which then had no exit once the stop had
started. A hold the cutover created has an empty cohort: no agent has a
continuation there, and no failure under it gates the go/no-go release.

A member's receipts are unchanged, a cancelled wake of an already drained
member included: that one still latches after the stop started, a gap the
[cutover runbook](../../conventions/cutover-home-adoption.md#a-legacy-stop-hold-with-failure-receipts)
records. The cutover adoption settles the legacy receipts outside the cohort
(`MaintenanceHold.receipts_outside_cohort`).

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, grading of failure receipts and the repair exit.
