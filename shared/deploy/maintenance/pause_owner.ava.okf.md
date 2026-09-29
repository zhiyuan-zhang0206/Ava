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
one keeps business closed, and `ava cluster recover` clears only its captured
exact record, after the no-live-owner proof (a pending or live local updater
handoff refuses) and a successful unpause. An unreadable journal needs that
proof before recovery force-clears it.

An explicit [maintenance hold](maintenance.ava.okf.md) uses the same journal
with a typed cohort/progress payload and the recorded shepherding process it
was taken under (`shared/deploy/maintenance/hold_driver.py`). It has no expiry timer and no
automatic release (see
[[shared/deploy/state/stranded-hold-recovery.ava.okf.md]]); only its exact
operation's explicit `ava maintenance resume` (or `resume --cancel`) ends it; the fleet
cutover's hold ends only through the cutover's go/no-go step (`cli/cutover_hold.py`). Ordinary
compensation, force-clear and a newer rollout cannot release or overwrite it.
This is distinct from a retired updater's pause record, which recovery clears.
