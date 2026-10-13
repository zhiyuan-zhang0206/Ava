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

Only a maintenance hold (below) writes the journal. A record without a hold
and an unreadable journal both read as invalid: they keep business closed and
refuse new work, and no command clears them. They have one exit: after confirming no `ava
stop` is in flight for this home, an operator
removes the file by hand (`rm $AVA_HOME/run/deploy-pause-owner.json`) and runs
`ava start`.

An explicit [maintenance hold](maintenance.ava.okf.md) uses the same journal
with a typed cohort/progress payload. The journal records no process identity:
whether another `ava stop` is still running is answered by the lifecycle lock and
`run/lifecycle-op.json`, not by the hold. A journal written by an earlier release
may carry a `driver` key; it is ignored on read and absent after the next write
([why](../../../../docs/decisions/runtime/processes/shutdown/2026-10-13-delete-hold-driver-diagnostics.md)).
It has no expiry timer and no automatic release; only its exact
operation's `ava start` ends it. Ordinary
compensation and a newer maintenance operation cannot release or overwrite it.
