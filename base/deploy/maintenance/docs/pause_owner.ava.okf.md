---
type: doc
title: Deploy pause owner
description: Host-local exact capability journal for DB-independent pause compensation.
status: current
---

# Deploy pause owner

`$AVA_HOME/run/deploy-pause-owner.json` records the exact capability
`(holder, acquired_at)` that paused this host. Resume never mints or rereads a
current DB capability: it matches only the local journal, so it still works
while the gateway or Postgres is unavailable and a delayed generation A resume
cannot unpause generation B.

Only a maintenance hold (below) writes the journal. A `paused` or `resumed`
record without a hold is what the retired updater's stop op left: a `paused`
one keeps business closed, and no command clears it. An unreadable journal
refuses new work the same way. Both have one exit: after confirming no `ava
stop` or `ava maintenance` command is in flight for this home, an operator
removes the file by hand (`rm $AVA_HOME/run/deploy-pause-owner.json`) and runs
`ava start`.

An explicit [maintenance hold](maintenance.ava.okf.md) uses the same journal
with a typed cohort/progress payload and the recorded shepherding process it
was taken under (`base/deploy/maintenance/hold_driver.py`). It has no expiry timer and no
automatic release (see
[[host_deploy_state/stranded-hold-recovery.ava.okf.md]]); only its exact
operation's `ava start` (or `ava maintenance cancel`) ends it. Ordinary
compensation and a newer maintenance operation cannot release or overwrite it.
This is distinct from a retired updater's pause record, which has no exit but
the manual removal above.
