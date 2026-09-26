---
type: doc
title: Deploy pause owner
description: Host-local exact capability journal for DB-independent pause compensation.
status: current
---

# Deploy pause owner

`$AVA_HOME/run/deploy-pause-owner.json` records the exact central deploy-lease
identity `(holder, acquired_at)` that most recently paused this host. A stop
request carries that identity; under the lifecycle mutex the runner verifies it
against the live executing lease, atomically journals `paused`, verifies the
lease again, then mutates posture. A mismatch or replacement writes no posture.

Resume never mints or rereads a current DB capability. It matches only the
local journal, so it still works while the gateway or Postgres is unavailable
and a delayed generation A resume cannot unpause generation B. Matching
`paused` unpauses then records `resumed`; matching `resumed` is an idempotent
no-op. Before unpausing, a fresh pending or live running local updater handoff
blocks a stale deploy resume. Recovery clears only its captured exact journal
after the normal no-live-owner proof and successful unpause.

The first-adoption bridge is retired: an empty resume payload no longer routes
to a legacy path — every resume carries the exact transition payload, and an
empty payload fails closed (no pre-protocol orchestrator remains; fleet
verified 2026-09-20).

The control-plane stop/resume payload requires both fields and a timezone-aware
RFC3339 timestamp. Missing, naive, or mismatched capabilities fail closed. Old
receivers ignore the new payload; a new receiver refuses an old tokenless
resume. Full delayed-request protection begins after stop-side protocol
adoption on every node.


An explicit [maintenance hold](maintenance/maintenance.ava.okf.md) uses the same journal
with a typed cohort/progress payload and the recorded shepherding process it
was taken under (`shared/hold_driver.py`). It has no expiry timer and no
automatic release (see
[[host_deploy_state/stranded-hold-recovery.ava.okf.md]]); only its exact
operation's explicit `ava maintenance resume` (or `resume --cancel`) ends it. Ordinary
compensation, force-clear and a newer rollout cannot release or overwrite it.
This is distinct from a recoverable stranded rollout pause.
